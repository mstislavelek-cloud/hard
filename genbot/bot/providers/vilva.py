"""Vilva через их удалённый MCP-сервер (Streamable HTTP, JSON-RPC).

Генерация: generate_image (обычно сразу URL), generate_video (generationId → check_generation).
Агент: agent_create_run (plan_first / autopilot), agent_get_run, agent_get_plan, agent_respond,
agent_cancel_run. Имена аргументов берутся из inputSchema инструментов (tools/list), поэтому
клиент подстраивается под фактическую схему Vilva.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .base import GenResult, Media, ProviderError
from .util import find_first_url, find_key

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
DONE = {"completed", "succeeded", "success", "done", "ready", "finished"}
FAILED = {"failed", "error", "errored"}
CANCELLED = {"cancelled", "canceled", "rejected"}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXT = (".mp4", ".webm", ".mov")

# Смысловое поле → возможные имена аргумента у Vilva.
ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt", "brief", "task", "instructions", "message", "text", "input"),
    "model": ("model", "modelId", "model_id", "modelSlug"),
    "image": ("imageUrl", "image_url", "image", "inputImage", "startImage", "referenceImage", "sourceImage"),
    "aspect_ratio": ("aspectRatio", "aspect_ratio", "ratio"),
    "resolution": ("resolution", "quality", "size"),
    "duration": ("duration", "durationSeconds", "seconds", "length"),
    "seed": ("seed",),
    "generation_id": ("generationId", "generation_id", "id"),
    "run_id": ("runId", "run_id", "id"),
    "mode": ("mode", "runMode", "run_mode"),
    "budget": ("creditBudget", "budget", "maxCredits", "creditLimit", "budgetCredits", "credit_budget"),
    "agent": ("agentSlug", "agent_slug", "agentId", "agent_id"),
    "decision": ("action", "decision", "approval", "approve"),
    "answer": ("response", "answer", "message", "text", "reply"),
}


def build_args(schema: dict | None, values: dict[str, Any]) -> dict:
    """Раскладывает значения по именам аргументов из inputSchema инструмента."""
    props = (schema or {}).get("properties") or {}
    args: dict = {}
    for semantic, value in values.items():
        if value is None or value == "":
            continue
        names = ALIASES.get(semantic, (semantic,))
        name = next((n for n in names if n in props), None)
        if name is None:
            if props:
                # Схема известна, а такого поля нет: не шлём, чтобы не получить ошибку валидации.
                if semantic in ("prompt", "run_id", "generation_id"):
                    name = names[0]
                else:
                    continue
            else:
                name = names[0]
        if name == "approve" and isinstance(value, str):
            value = value == "approve"
        args[name] = value
    return args


class McpClient:
    """Минимальный MCP-клиент: initialize + tools/list + tools/call поверх HTTP (JSON или SSE)."""

    def __init__(self, url: str, key: str, session: aiohttp.ClientSession | None = None) -> None:
        self.url = url
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._session = session
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self.schemas: dict[str, dict] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, payload: dict, expect_response: bool = True) -> dict | None:
        session = await self._get_session()
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        async with session.post(self.url, json=payload, headers=headers) as r:
            if r.status == 404 and self._session_id:
                # Сессия истекла: переподключаемся.
                self._session_id, self._initialized = None, False
                raise _SessionExpired
            if r.status >= 400:
                raise ProviderError(f"vilva mcp {r.status}: {(await r.text())[:500]}", code=f"http_{r.status}")
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            if not expect_response:
                return None
            ctype = r.headers.get("Content-Type", "")
            text = await r.text()
        message = _parse_sse(text, payload.get("id")) if "text/event-stream" in ctype else json.loads(text)
        if message is None:
            raise ProviderError("vilva mcp: пустой ответ")
        if "error" in message:
            raise ProviderError(f"vilva mcp error: {message['error']}", code="rpc_error")
        return message.get("result")

    async def _ensure_initialized(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            await self._post({
                "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "genbot", "version": "2.0"},
                },
            })
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
            self._initialized = True
            try:
                result = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/list"})
                self.schemas = {t["name"]: t.get("inputSchema") or {} for t in (result or {}).get("tools", [])}
            except ProviderError as e:
                log.warning("vilva tools/list: %s", e)

    async def list_tools(self) -> dict[str, dict]:
        await self._ensure_initialized()
        return self.schemas

    async def call_tool(self, name: str, values: dict[str, Any]) -> dict:
        """values — смысловые поля (prompt, model, run_id…), имена берутся из схемы инструмента."""
        for attempt in range(2):
            await self._ensure_initialized()
            args = build_args(self.schemas.get(name), values)
            try:
                result = await self._post({
                    "jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                })
            except _SessionExpired:
                if attempt == 0:
                    continue
                raise ProviderError("vilva: сессия истекла", code="session")
            if result is None:
                raise ProviderError(f"vilva {name}: пустой результат")
            if result.get("isError"):
                text = _text(result)
                code = "insufficient_credits" if "credit" in text.lower() else "tool_error"
                raise ProviderError(f"vilva {name}: {text[:500]}", code=code)
            return result
        raise ProviderError("vilva: не удалось вызвать инструмент")


class _SessionExpired(Exception):
    pass


def _parse_sse(text: str, want_id) -> dict | None:
    """Достаёт из SSE-потока JSON-RPC ответ с нужным id."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if want_id is None or msg.get("id") == want_id:
            return msg
    return None


def _text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def payload(result: dict) -> Any:
    """structuredContent, если есть, иначе контент (текст часто содержит JSON)."""
    if result.get("structuredContent"):
        return result["structuredContent"]
    items = []
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                items.append(json.loads(c.get("text", "")))
            except ValueError:
                items.append(c.get("text", ""))
        else:
            items.append(c)
    return items[0] if len(items) == 1 else items


def _status(obj: Any) -> str:
    return str(find_key(obj, "status", "state") or "").lower()


def _credits_used(obj: Any) -> float | None:
    v = find_key(obj, "creditsUsed", "creditsSpent", "credits_used", "spentCredits", "creditsCharged", "cost")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class VilvaProvider:
    def __init__(self, key: str = "", url: str = "https://api.vilva.ai/mcp", client: McpClient | None = None,
                 poll_interval: float = 4.0, timeout: float = 900.0) -> None:
        if not key and client is None:
            raise ValueError("VILVA_API_KEY не задан")
        self.client = client or McpClient(url, key)
        self.poll_interval = poll_interval
        self.timeout = timeout

    async def close(self) -> None:
        await self.client.close()

    # --- модели ---

    async def discover_models(self) -> list:
        """Модели из list_models с ценами; параметры — из enum в схемах generate_*."""
        from ..catalog import ModelSpec

        schemas = await self.client.list_tools()
        data = payload(await self.client.call_tool("list_models", {}))
        items = _model_items(data)
        specs = []
        for item in items:
            mid = str(item.get("id") or item.get("slug") or item.get("model") or item.get("name") or "")
            if not mid:
                continue
            kind = _model_kind(item)
            if kind not in ("image", "video"):
                continue
            tool = "generate_image" if kind == "image" else "generate_video"
            options = _schema_options(schemas.get(tool))
            for key in ("aspect_ratio", "resolution", "duration"):
                vals = item.get(key + "s") or item.get(_camel(key) + "s")
                if isinstance(vals, list) and vals:
                    options[key] = tuple(str(v) for v in vals)
            cost = _credits_used({"cost": find_key(item, "credits", "cost", "price", "creditCost", "costCredits")})
            specs.append(ModelSpec(
                key=f"vilva:{mid}", provider="vilva", kind=kind,
                title=str(item.get("name") or item.get("title") or mid), arch=mid,
                base_units=cost or 1.0, options=options,
                simple={k: v[0] for k, v in options.items() if k == "aspect_ratio"},
                image_field="image" if kind == "video" or _accepts_image(schemas.get(tool)) else None,
                note=str(item.get("description") or "")[:80],
            ))
        return specs

    # --- генерация ---

    async def generate(self, spec, prompt, params, image) -> GenResult:
        tool = "generate_image" if spec.kind == "image" else "generate_video"
        prefer = IMAGE_EXT if spec.kind == "image" else VIDEO_EXT
        values: dict[str, Any] = {"prompt": prompt, "model": spec.arch, **params}
        if image is not None:
            values["image"] = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        result = await self.client.call_tool(tool, values)
        data = payload(result)
        units = _credits_used(data)
        url = _media_url(data, prefer)
        if not url:
            gen_id = find_key(data, "generationId", "generation_id")
            if not gen_id:
                raise ProviderError(f"vilva: нет результата: {_text(result)[:300]}")
            data = await self._wait_generation(str(gen_id))
            url = _media_url(data, prefer) or find_first_url(data)
            units = _credits_used(data) or units
            if not url:
                raise ProviderError("vilva: готово, но нет url")
        ext = "png" if spec.kind == "image" else "mp4"
        return GenResult(Media(url=url, filename=f"{spec.kind}.{ext}"), units)

    async def _wait_generation(self, gen_id: str) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            data = payload(await self.client.call_tool("check_generation", {"generation_id": gen_id}))
            status = _status(data)
            if status in FAILED or status in CANCELLED:
                raise ProviderError(f"vilva: генерация {status}", code="generation_failed")
            if status in DONE or (not status and find_first_url(data)):
                return data
            if loop.time() > deadline:
                raise ProviderError("vilva timeout", code="timeout")
            await asyncio.sleep(self.poll_interval)

    # --- агент ---

    async def agent_create(self, brief: str, mode: str, budget: float, agent: str | None = None) -> str:
        data = payload(await self.client.call_tool(
            "agent_create_run", {"prompt": brief, "mode": mode, "budget": budget, "agent": agent}
        ))
        run_id = find_key(data, "runId", "run_id", "id")
        if not run_id:
            raise ProviderError(f"vilva agent: нет runId: {data}")
        return str(run_id)

    async def agent_get(self, run_id: str) -> "AgentState":
        data = payload(await self.client.call_tool("agent_get_run", {"run_id": run_id}))
        return AgentState.from_payload(run_id, data)

    async def agent_plan(self, run_id: str) -> Any:
        return payload(await self.client.call_tool("agent_get_plan", {"run_id": run_id}))

    async def agent_decide(self, run_id: str, approve: bool) -> None:
        decision = "approve" if approve else "reject"
        schema = self.client.schemas.get("agent_get_plan") or {}
        props = schema.get("properties") or {}
        if any(n in props for n in ALIASES["decision"]):
            await self.client.call_tool("agent_get_plan", {"run_id": run_id, "decision": decision})
        else:
            await self.client.call_tool("agent_respond", {"run_id": run_id, "answer": decision})

    async def agent_answer(self, run_id: str, text: str) -> None:
        await self.client.call_tool("agent_respond", {"run_id": run_id, "answer": text})

    async def agent_cancel(self, run_id: str) -> None:
        await self.client.call_tool("agent_cancel_run", {"run_id": run_id})


@dataclass
class AgentState:
    run_id: str
    phase: str            # running | plan | question | done | failed | cancelled
    raw_status: str
    text: str = ""
    credits_used: float | None = None
    urls: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, run_id: str, data: Any) -> "AgentState":
        status = _status(data)
        phase = "running"
        if status in DONE:
            phase = "done"
        elif status in FAILED:
            phase = "failed"
        elif status in CANCELLED:
            phase = "cancelled"
        elif "plan" in status or "approv" in status or "budget" in status:
            phase = "plan"
        elif "question" in status or "input" in status or "waiting" in status or "awaiting" in status:
            phase = "question"
        text = str(find_key(data, "question", "message", "summary", "output", "result") or "")
        urls = sorted(set(_all_urls(data)), key=lambda u: (not u.lower().split("?")[0].endswith(VIDEO_EXT + IMAGE_EXT), u))
        return cls(run_id, phase, status, text if isinstance(text, str) else "", _credits_used(data), urls)


def _all_urls(obj: Any) -> list[str]:
    from .util import _iter_urls

    return list(_iter_urls(obj))


def _media_url(data: Any, prefer: tuple[str, ...]) -> str | None:
    url = find_first_url(data, prefer)
    if url and url.lower().split("?", 1)[0].endswith(prefer):
        return url
    # Ссылка без расширения тоже годится, если явно лежит в поле url/imageUrl/videoUrl.
    direct = find_key(data, "url", "imageUrl", "videoUrl", "image_url", "video_url", "assetUrl")
    return direct if isinstance(direct, str) and direct.startswith("http") else None


def _model_items(data: Any) -> list[dict]:
    if isinstance(data, dict):
        for k in ("models", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
        # {"image": [...], "video": [...]}
        out = []
        for k, v in data.items():
            if isinstance(v, list):
                out.extend({**x, "type": x.get("type", k)} for x in v if isinstance(x, dict))
        return out
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _model_kind(item: dict) -> str:
    t = str(item.get("type") or item.get("kind") or item.get("category") or item.get("media") or "").lower()
    if "video" in t:
        return "video"
    if "image" in t or "img" in t:
        return "image"
    return t


def _camel(s: str) -> str:
    head, *rest = s.split("_")
    return head + "".join(p.title() for p in rest)


def _schema_options(schema: dict | None) -> dict[str, tuple[str, ...]]:
    props = (schema or {}).get("properties") or {}
    options = {}
    for semantic in ("aspect_ratio", "resolution", "duration"):
        for name in ALIASES[semantic]:
            enum = (props.get(name) or {}).get("enum")
            if enum:
                options[semantic] = tuple(str(v) for v in enum)
                break
    return options


def _accepts_image(schema: dict | None) -> bool:
    props = (schema or {}).get("properties") or {}
    return any(n in props for n in ALIASES["image"])
