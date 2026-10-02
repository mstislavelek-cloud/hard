"""Проверка подключения провайдеров без запуска бота: python -m bot.check

Vilva: инструменты со схемами, баланс, модели (кредиты не тратит).
Mage: баланс не тратится, проверяется только ключ и доступность API.
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp

from .config import load_dotenv
from .providers.vilva import McpClient, VilvaProvider, _text


async def check_vilva(url: str, key: str) -> None:
    client = McpClient(url, key)
    try:
        schemas = await client.list_tools()
        print(f"vilva: инструментов {len(schemas)}")
        for name, schema in schemas.items():
            print(f"  - {name}: {json.dumps(schema.get('properties', {}), ensure_ascii=False)[:400]}")
        for name in ("get_credit_balance", "list_models"):
            try:
                text = _text(await client.call_tool(name, {}))
                print(f"vilva {name}: {text[:1500]}{' …' if len(text) > 1500 else ''}")
                if name == "list_models":
                    with open("vilva_models.json", "w", encoding="utf-8") as f:
                        f.write(text)
                    print("vilva: полный список моделей сохранён в vilva_models.json")
            except Exception as e:
                print(f"vilva {name}: ошибка {e}")
        specs = await VilvaProvider(client=client).discover_models()
        print("vilva: модели в боте:")
        for s in specs:
            print(f"  - {s.kind} {s.key} «{s.title}» база {s.base_units}, таблицы {s.price_table}, "
                  f"за сек {s.per_second or s.rate_by_res}, параметры {s.options}")
    finally:
        await client.close()


async def check_mage(base: str, key: str) -> None:
    # Несуществующая архитектура: ничего не списывается, по коду ответа видно, принят ли ключ.
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/__genbot_check__/generate", json={"prompt": "x"},
                          headers={"Authorization": f"Bearer {key}"}) as r:
            body = await r.text()
            verdict = {401: "ключ не принят", 404: "ключ принят (архитектуры нет — так и задумано)"}
            print(f"mage: HTTP {r.status} — {verdict.get(r.status, body[:300])}")


def _run(name: str, coro) -> None:
    try:
        asyncio.run(coro)
    except Exception as e:
        print(f"{name}: ошибка подключения — {e}")


def main() -> None:
    load_dotenv()
    if os.getenv("VILVA_API_KEY"):
        _run("vilva", check_vilva(os.getenv("VILVA_MCP_URL", "https://api.vilva.ai/mcp"), os.environ["VILVA_API_KEY"]))
    else:
        print("vilva: VILVA_API_KEY не задан")
    if os.getenv("MAGE_API_KEY"):
        _run("mage", check_mage(os.getenv("MAGE_BASE_URL", "https://api.mage.space/v1"), os.environ["MAGE_API_KEY"]))
    else:
        print("mage: MAGE_API_KEY не задан")


if __name__ == "__main__":
    main()
