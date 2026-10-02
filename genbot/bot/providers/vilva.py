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
import re
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
    "duration": ("duration", "durationSeconds", "durationSec", "seconds", "length"),
    "seed": ("seed",),
    "effort": ("effort", "quality", "reasoningEffort"),
    "generation_id": ("generationId", "generation_id", "id"),
    "run_id": ("runId", "run_id", "id"),
    "mode": ("mode", "runMode", "run_mode"),
    "budget": ("creditBudget", "budget", "maxCredits", "creditLimit", "budgetCredits", "credit_budget"),
    "agent": ("agentSlug", "agent_slug", "agentId", "agent_id"),
    "decision": ("action", "decision", "approval", "approve"),
    "answer": ("response", "answer", "message", "text", "reply"),
    "resume_key": ("resumeKey", "resume_key", "pauseKey"),
    "workspace": ("workspaceId", "workspace_id", "workspace"),
    "answers": ("answers", "responses", "fields", "values", "formAnswers"),
    "defaults": ("useDefaults", "acceptDefaults", "useAgentChoices", "continueWithDefaults", "skip"),
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
        args[name] = _coerce(props.get(name), value)
    return args


def _coerce(prop: dict | None, value: Any) -> Any:
    """Строку из настроек бота приводит к типу из схемы: флаг, число или исходное значение enum."""
    if not isinstance(prop, dict) or not isinstance(value, str):
        return value
    enums = list(prop.get("enum") or [])
    for alt in prop.get("anyOf") or prop.get("oneOf") or ():
        if isinstance(alt, dict):
            enums += alt.get("enum") or []
    for e in enums:
        if (str(e).lower() if isinstance(e, bool) else str(e)) == value:
            return e
    kind = prop.get("type")
    if kind == "boolean" and value in ("true", "false"):
        return value == "true"
    if kind in ("integer", "number"):
        try:
            num = float(value)
            return int(num) if kind == "integer" or num.is_integer() else num
        except ValueError:
            return value
    return value


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

    async def call_tool(self, name: str, values: dict[str, Any], raw: dict[str, Any] | None = None) -> dict:
        """values — смысловые поля (prompt, model, run_id…), имена берутся из схемы инструмента.
        raw — аргументы с точными именами, добавляются как есть."""
        for attempt in range(2):
            await self._ensure_initialized()
            args = build_args(self.schemas.get(name), values)
            if raw:
                args.update(raw)
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
                code = _error_code(text)
                log.warning("vilva %s отказал (%s), аргументы: %s", name, code, _loggable(args))
                user = None
                if code == "invalid_params":
                    user = "Модель не приняла параметры — попробуй другую длительность или разрешение"
                raise ProviderError(f"vilva {name}: {text[:500]}", code=code, user_message=user)
            return result
        raise ProviderError("vilva: не удалось вызвать инструмент")


def _error_code(text: str) -> str:
    """Код ошибки Vilva. «Insufficient credits … Required: 0, Available: 2978» — это не пустой баланс:
    Vilva не смогла посчитать цену, то есть не приняла параметры запроса."""
    low = text.lower()
    if "credit" not in low:
        return "tool_error"
    req = re.search(r"required\D*([\d.]+)", low)
    avail = re.search(r"available\D*([\d.]+)", low)
    if req and avail and float(req.group(1)) <= float(avail.group(1)):
        return "invalid_params"
    return "insufficient_credits"


def _loggable(args: dict) -> dict:
    """Аргументы для лога без base64-картинок."""
    return {k: (v[:40] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in args.items()}


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
    credits = obj.get("credits") if isinstance(obj, dict) else None
    if isinstance(credits, dict) and isinstance(credits.get("used"), (int, float)):
        return float(credits["used"])
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
        # run_id → resumeKey активной паузы (нужен agent_respond, чтобы продолжить запуск)
        self.resume_keys: dict[str, str] = {}
        self.poll_interval = poll_interval
        self.timeout = timeout

    async def close(self) -> None:
        await self.client.close()

    # --- модели ---

    async def discover_models(self) -> list:
        """Модели из list_models с ценами; параметры — из enum в схемах generate_*."""
        from ..catalog import ModelSpec
        from ..vilva_prices import apply_doc_prices

        schemas = await self.client.list_tools()
        data = payload(await self.client.call_tool("list_models", {}))
        items = _model_items(data)
        specs = []
        for item in items:
            mid = str(item.get("key") or item.get("id") or item.get("slug") or item.get("model") or "")
            if not mid:
                continue
            kind = _model_kind(item)
            if kind not in ("image", "video"):
                continue
            tool = "generate_image" if kind == "image" else "generate_video"
            options = _schema_options(schemas.get(tool))
            for key in list(options):
                vals = (item.get(key + "s") or item.get(_camel(key) + "s") or item.get(key + "Options")
                        or item.get(_camel(key) + "Options"))
                if isinstance(vals, list) and vals and all(isinstance(v, (str, int, float)) for v in vals):
                    options[key] = tuple(str(v) for v in vals)
            for key in ("aspect_ratio", "resolution"):
                vals = item.get(key + "s") or item.get(_camel(key) + "s")
                if isinstance(vals, list) and vals:
                    options[key] = tuple(str(v) for v in vals)
            if kind == "video":
                durations = _item_durations(item)
                if durations:
                    options["duration"] = durations
            base, table, per_second, rate_by_res = _parse_credits(item)
            simple = {}
            if "aspect_ratio" in options:
                simple["aspect_ratio"] = next(
                    (a for a in (("1:1",) if kind == "image" else ("9:16", "16:9")) if a in options["aspect_ratio"]),
                    options["aspect_ratio"][0])
            if "resolution" in options:
                simple["resolution"] = options["resolution"][0]
            if "duration" in options:
                simple["duration"] = "5" if "5" in options["duration"] else options["duration"][0]
            if _is_utility(mid):
                continue
            rate_by_res = _rates_for_resolutions(rate_by_res, options.get("resolution", ()))
            refs = item.get("maxReferenceImages")
            accepts = _accepts_image(schemas.get(tool)) or (isinstance(refs, int) and refs > 0)
            specs.append(apply_doc_prices(ModelSpec(
                key=f"vilva:{mid}", provider="vilva", kind=kind,
                title=str(item.get("displayName") or item.get("name") or item.get("title") or mid), arch=mid,
                base_units=base or 1.0, options=options, simple=simple,
                image_field="image" if accepts or kind == "video" else None,
                note=str(item.get("description") or "")[:80],
                price_table=table, per_second=per_second, rate_by_res=rate_by_res,
            )))
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
        state = AgentState.from_payload(run_id, data)
        if state.resume_key:
            self.resume_keys[run_id] = state.resume_key
        else:
            self.resume_keys.pop(run_id, None)
        return state

    async def agent_plan(self, run_id: str) -> Any:
        return payload(await self.client.call_tool("agent_get_plan", {"run_id": run_id}))

    def _answer_arg(self) -> tuple[str, str | None]:
        """Имя и тип поля ответа в agent_respond (у Vilva это объект answer)."""
        props = (self.client.schemas.get("agent_respond") or {}).get("properties") or {}
        name = next((n for n in ("answer", "response", "answers", "reply", "message", "text") if n in props), "answer")
        return name, (props.get(name) or {}).get("type")

    async def _respond(self, run_id: str, text: str, obj: dict) -> None:
        name, typ = self._answer_arg()
        value: Any = text if typ == "string" else obj
        await self.client.call_tool(
            "agent_respond", {"run_id": run_id, "resume_key": self.resume_keys.get(run_id)}, raw={name: value})

    async def agent_decide(self, run_id: str, approve: bool) -> None:
        decision = "approve" if approve else "reject"
        if self.resume_keys.get(run_id):
            await self._respond(run_id, decision, {"approved": approve, "decision": decision})
            return
        schema = self.client.schemas.get("agent_get_plan") or {}
        props = schema.get("properties") or {}
        if any(n in props for n in ALIASES["decision"]):
            await self.client.call_tool("agent_get_plan", {"run_id": run_id, "decision": decision})
        else:
            await self._respond(run_id, decision, {"approved": approve, "decision": decision})

    async def agent_answer(self, run_id: str, text: str, answers: dict[str, Any] | None = None) -> None:
        """Ответ агенту. Анкета Vilva ждёт объект {id вопроса: значение}; простой вопрос — {"text": ...}."""
        await self._respond(run_id, text, answers if answers else {"text": text})

    async def workspace_assets(self, workspace_id: str) -> Any:
        return payload(await self.client.call_tool("list_workspace_assets", {"workspace": workspace_id}))

    async def agent_cancel(self, run_id: str) -> None:
        await self.client.call_tool("agent_cancel_run", {"run_id": run_id})


@dataclass
class Question:
    id: str
    text: str
    options: list[str] = field(default_factory=list)   # подписи
    required: bool = False
    hint: str = ""
    values: list[str] = field(default_factory=list)    # значения для ответа (по умолчанию = подписи)
    qtype: str = ""                                    # text | single-choice | multi-choice | file-upload
    multiple: bool = False

    def value_of(self, i: int) -> str:
        return self.values[i] if i < len(self.values) else self.options[i]


QUESTION_TEXT = ("question", "label", "title", "prompt", "text", "name")
QUESTION_OPTIONS = ("options", "choices", "suggestions", "suggestedAnswers", "answers", "values")
QUESTION_LISTS = ("questions", "pendingQuestions", "fields", "form")


def _option_label(o: Any) -> str:
    if isinstance(o, dict):
        return str(o.get("label") or o.get("title") or o.get("text") or o.get("value") or "")
    return str(o)


def _as_question(d: dict, idx: int) -> Question | None:
    text = next((d[k] for k in QUESTION_TEXT if isinstance(d.get(k), str) and d[k].strip()), None)
    if not text:
        return None
    opts = next((d[k] for k in QUESTION_OPTIONS if isinstance(d.get(k), list)), [])
    options, values = [], []
    for o in opts:
        lbl = _option_label(o)
        if lbl:
            options.append(lbl)
            values.append(str(o.get("value", lbl)) if isinstance(o, dict) else lbl)
    qid = str(d.get("id") or d.get("key") or d.get("questionId") or d.get("name") or idx)
    hint = d.get("help") or d.get("description") or d.get("hint") or d.get("helpText") or d.get("placeholder") or ""
    qtype = str(d.get("type") or ("single-choice" if options else "text")).lower()
    if qtype == "confirm" and not options:
        options = [str(d.get("yesLabel") or "Да"), str(d.get("noLabel") or "Нет")]
        values = ["true", "false"]
    multiple = "multi" in qtype or d.get("multiple") is True and "file" not in qtype
    return Question(qid, text.strip(), options, bool(d.get("required")), hint if isinstance(hint, str) else "",
                    values, qtype, bool(multiple))


def extract_questions(data: Any) -> list[Question]:
    """Ищет в ответе agent_get_run вопросы агента: список под questions/fields/… или объекты с options."""
    found: list[Question] = []
    seen: set[str] = set()

    def add(q: Question | None) -> None:
        if q and q.text not in seen:
            seen.add(q.text)
            found.append(q)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k in QUESTION_LISTS:
                if isinstance(obj.get(k), list) and obj[k] and all(isinstance(x, dict) for x in obj[k]):
                    for i, item in enumerate(obj[k]):
                        add(_as_question(item, len(found) + i))
            if any(isinstance(obj.get(k), list) for k in QUESTION_OPTIONS) and any(
                    isinstance(obj.get(k), str) for k in QUESTION_TEXT):
                add(_as_question(obj, len(found)))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    # Одиночный вопрос строкой: {"status": "waiting_for_input", "question": "..."}
    if not found and isinstance(data, dict):
        q = find_key(data, "question", "pendingQuestion")
        if isinstance(q, str) and q.strip():
            found.append(Question("0", q.strip()))
    return found


@dataclass
class AgentState:
    run_id: str
    phase: str            # running | plan | question | done | failed | cancelled
    raw_status: str
    text: str = ""
    credits_used: float | None = None
    urls: list[str] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    raw: Any = None
    pause_tool: str = ""
    resume_key: str = ""

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
        pause = data.get("activePause") if isinstance(data, dict) else None
        pause_tool, resume_key = "", ""
        if isinstance(pause, dict) and phase not in ("done", "failed", "cancelled"):
            pause_tool = str(pause.get("pauseTool") or pause.get("tool") or "")
            resume_key = str(pause.get("resumeKey") or "")
            tool = pause_tool.lower()
            phase = "question" if ("ask" in tool or "question" in tool or "input" in tool) else "plan"
        questions = extract_questions(data) if phase not in ("done", "failed", "cancelled") else []
        if questions and phase in ("running", "plan"):
            phase = "question"
        text = str(find_key(data, "question", "message", "summary", "output", "result") or "")
        urls = sorted(set(_all_urls(data)), key=lambda u: (not u.lower().split("?")[0].endswith(VIDEO_EXT + IMAGE_EXT), u))
        return cls(run_id, phase, status, text if isinstance(text, str) else "", _credits_used(data), urls,
                   questions, data, pause_tool, resume_key)


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


def _snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _parse_credits(item: dict) -> tuple[float, dict[str, dict[str, float]], float, dict[str, float]]:
    """credits: число или {"base": 12, "perResolution": {"1K": 12}, "perSecond": 3 | {"720p": 10}}."""
    raw = None
    for k in ("credits", "cost", "price", "creditCost", "costCredits"):
        if item.get(k) is not None:
            raw = item[k]
            break
    if isinstance(raw, (int, float)):
        return float(raw), {}, 0.0, {}
    if not isinstance(raw, dict):
        return 0.0, {}, 0.0, {}
    table: dict[str, dict[str, float]] = {}
    per_second = 0.0
    rate_by_res: dict[str, float] = {}
    for k, v in raw.items():
        if k in ("perSecond", "per_second", "perSec") and isinstance(v, (int, float)):
            per_second = float(v)
        elif k in ("perSecond", "per_second", "perSec", "perSecondByResolution") and isinstance(v, dict):
            rate_by_res = {str(a): float(b) for a, b in v.items() if isinstance(b, (int, float))}
        elif k.startswith("per") and isinstance(v, dict):
            name = _snake(k[3:].lstrip("_"))
            table[name] = {str(a): float(b) for a, b in v.items() if isinstance(b, (int, float))}
    base = raw.get("base")
    if not isinstance(base, (int, float)):
        base = min((min(t.values()) for t in table.values() if t), default=0.0)
    return float(base), table, per_second, rate_by_res


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


# Инструменты Vilva, которым нужен исходник (видео, аудио, фото для апскейла) — в выборе моделей не показываем.
UTILITY_MODELS = ("remove-background", "upscale", "heygen-translate", "heygen-lipsync", "heygen-avatar",
                  "bytedance-asset", "video-edit", "video-extend", "motion-control", "kling-avatar")


def _is_utility(model_id: str) -> bool:
    return any(u in model_id for u in UTILITY_MODELS)


# Тарифы Kling называются std/pro/4K, а не разрешениями; со звуком дороже — берём ставку со звуком (не в минус).
TIER_RES = {"std": "720p", "pro": "1080p", "4k": "4k"}


def _rates_for_resolutions(rates: dict[str, float], resolutions) -> dict[str, float]:
    if not rates or not resolutions or any(r in rates for r in resolutions):
        return rates
    out: dict[str, float] = {}
    for key, rate in rates.items():
        tier = key.lower().removesuffix("-audio")
        res = TIER_RES.get(tier)
        if res:
            match = next((r for r in resolutions if r.lower() == res), res)
            out[match] = max(out.get(match, 0.0), rate)
    if "default" in rates:
        out.setdefault("default", rates["default"])
    return out or rates


NICE_DURATIONS = (3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30)


def _durations_between(lo: float, hi: float) -> tuple[str, ...]:
    picked = [d for d in NICE_DURATIONS if lo <= d <= hi]
    if hi not in picked and hi <= 60:
        picked.append(int(hi))
    return tuple(str(d) for d in picked)


def _duration_range(prop) -> tuple[str, ...]:
    """Длительности из числового поля схемы с minimum/maximum (без enum)."""
    if not isinstance(prop, dict):
        return ()
    for alt in (prop, *(prop.get("anyOf") or ()), *(prop.get("oneOf") or ())):
        if isinstance(alt, dict) and isinstance(alt.get("maximum"), (int, float)):
            lo = alt.get("minimum") if isinstance(alt.get("minimum"), (int, float)) else 1
            return _durations_between(float(lo), float(alt["maximum"]))
    return ()


def _item_durations(item: dict) -> tuple[str, ...]:
    """Длительности из описания модели в list_models: список, min/max или «up to 30s» в описании."""
    for k in ("durations", "durationOptions", "supportedDurations", "allowedDurations"):
        vals = item.get(k)
        if isinstance(vals, list) and vals and all(isinstance(v, (int, float, str)) for v in vals):
            return tuple(str(v).rstrip("s") for v in vals)
    rng = item.get("durationRange") or item.get("duration")
    if isinstance(rng, dict):
        hi = rng.get("max") or rng.get("maximum")
        lo = rng.get("min") or rng.get("minimum") or 1
        if isinstance(hi, (int, float)):
            return _durations_between(float(lo), float(hi))
    hi = item.get("maxDuration") or item.get("maxDurationSeconds") or item.get("maxSeconds")
    if isinstance(hi, (int, float)):
        lo = item.get("minDuration") or item.get("minDurationSeconds") or 1
        return _durations_between(float(lo if isinstance(lo, (int, float)) else 1), float(hi))
    m = re.search(r"up to (\d+)\s*(?:s\b|sec|second)", str(item.get("description") or ""), re.I)
    if m:
        return _durations_between(4, float(m.group(1)))
    return ()


# Служебные поля generate_*, которые не показываем как настройки.
SKIP_PARAMS = {"wait", "async", "sync", "stream", "dryrun", "dry_run", "public", "private", "notify", "webhook",
               "webhookurl", "projectid", "workspaceid", "folderid"}


def _schema_options(schema: dict | None) -> dict[str, tuple[str, ...]]:
    """Параметры из inputSchema: формат/разрешение/длительность под общими именами, плюс все прочие enum и флаги."""
    props = (schema or {}).get("properties") or {}
    options: dict[str, tuple[str, ...]] = {}
    taken: set[str] = set()
    for semantic in ("aspect_ratio", "resolution", "duration"):
        for name in ALIASES[semantic]:
            enum = (props.get(name) or {}).get("enum")
            if enum:
                options[semantic] = tuple(str(v) for v in enum)
                taken.add(name)
                break
    if "duration" not in options:
        for name in ALIASES["duration"]:
            rng = _duration_range(props.get(name))
            if rng:
                options["duration"] = rng
                taken.add(name)
                break
    reserved = {n for key in ("prompt", "model", "image", "seed") for n in ALIASES[key]}
    for name, prop in props.items():
        if name in taken or name in reserved or name.lower() in SKIP_PARAMS or not isinstance(prop, dict):
            continue
        enum = prop.get("enum")
        if not enum:
            for alt in prop.get("anyOf") or prop.get("oneOf") or ():
                if isinstance(alt, dict) and alt.get("enum"):
                    enum = alt["enum"]
                    break
        if enum and len(enum) > 1:
            options[name] = tuple(str(v).lower() if isinstance(v, bool) else str(v) for v in enum if v is not None)
        elif prop.get("type") == "boolean":
            options[name] = ("true", "false")
    return options


def _accepts_image(schema: dict | None) -> bool:
    props = (schema or {}).get("properties") or {}
    return any(n in props for n in ALIASES["image"])
