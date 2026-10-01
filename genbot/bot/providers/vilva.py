"""Провайдер Vilva через их удалённый MCP-сервер (Streamable HTTP, JSON-RPC).

Инструменты по документации Vilva: generate_image (обычно сразу отдаёт URL),
generate_video (асинхронно: generationId → check_generation), list_models, get_credit_balance.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging

import aiohttp

from .base import Media, ProviderError
from .util import find_first_url, find_key

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
DONE = {"completed", "succeeded", "success", "done", "ready"}
FAILED = {"failed", "error", "cancelled", "canceled"}


class McpClient:
    """Минимальный MCP-клиент: initialize + tools/call поверх HTTP с ответами JSON или SSE."""

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

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
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
            if r.status >= 400:
                raise ProviderError(f"vilva mcp {r.status}: {await r.text()}")
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
            raise ProviderError(f"vilva mcp error: {message['error']}")
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
                    "clientInfo": {"name": "genbot", "version": "1.0"},
                },
            })
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
            self._initialized = True

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await self._ensure_initialized()
        result = await self._post({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if result is None:
            raise ProviderError(f"vilva {name}: пустой результат")
        if result.get("isError"):
            raise ProviderError(f"vilva {name}: {_text(result)[:500]}")
        return result

    async def list_tools(self) -> list[dict]:
        await self._ensure_initialized()
        result = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/list"})
        return (result or {}).get("tools", [])


def _parse_sse(text: str, want_id) -> dict | None:
    """Достаёт из SSE-потока JSON-RPC ответ с нужным id."""
    for block in text.split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        )
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


def _payload(result: dict) -> object:
    """structuredContent, если есть, иначе текстовый контент (часто это JSON)."""
    return result.get("structuredContent") or [c for c in result.get("content", [])]


class VilvaProvider:
    def __init__(
        self,
        key: str,
        url: str = "https://api.vilva.ai/mcp",
        image_model: str = "",
        video_model: str = "",
        client: McpClient | None = None,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
    ) -> None:
        if not key and client is None:
            raise ValueError("VILVA_API_KEY не задан")
        self.client = client or McpClient(url, key)
        self.image_model = image_model
        self.video_model = video_model
        self.poll_interval = poll_interval
        self.timeout = timeout

    async def close(self) -> None:
        await self.client.close()

    async def generate_image(self, prompt: str) -> Media:
        args = {"prompt": prompt}
        if self.image_model:
            args["model"] = self.image_model
        result = await self.client.call_tool("generate_image", args)
        url = find_first_url(_payload(result), (".png", ".jpg", ".jpeg", ".webp"))
        if not url:
            gen_id = find_key(_payload(result), "generationId", "generation_id")
            if not gen_id:
                raise ProviderError(f"vilva: нет картинки в ответе: {_text(result)[:300]}")
            url = await self._wait(gen_id, (".png", ".jpg", ".jpeg", ".webp"))
        return Media(url=url, filename="image.png")

    async def generate_video(self, prompt: str, image: bytes | None = None) -> Media:
        if image is not None:
            # Формат передачи картинки в generate_video по документации неизвестен.
            raise ProviderError("vilva: оживление фото пока не поддержано")
        args = {"prompt": prompt}
        if self.video_model:
            args["model"] = self.video_model
        result = await self.client.call_tool("generate_video", args)
        payload = _payload(result)
        url = find_first_url(payload, (".mp4", ".webm", ".mov"))
        if url and url.split("?", 1)[0].lower().endswith((".mp4", ".webm", ".mov")):
            return Media(url=url, filename="video.mp4")
        gen_id = find_key(payload, "generationId", "generation_id", "id")
        if not gen_id:
            raise ProviderError(f"vilva: нет generationId: {_text(result)[:300]}")
        url = await self._wait(gen_id, (".mp4", ".webm", ".mov"))
        return Media(url=url, filename="video.mp4")

    async def _wait(self, gen_id: str, prefer: tuple[str, ...]) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            result = await self.client.call_tool("check_generation", {"generationId": gen_id})
            payload = _payload(result)
            status = str(find_key(payload, "status") or "").lower()
            if status in FAILED:
                raise ProviderError(f"vilva: генерация {status}")
            url = find_first_url(payload, prefer)
            if status in DONE or (url and not status):
                if not url:
                    raise ProviderError("vilva: готово, но нет url")
                return url
            if loop.time() > deadline:
                raise ProviderError("vilva timeout")
            await asyncio.sleep(self.poll_interval)
