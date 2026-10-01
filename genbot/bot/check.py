"""Проверка подключения провайдеров без запуска бота: python -m bot.check

Vilva: выводит список инструментов, баланс и модели (кредиты не тратит).
Mage: показывает, куда бот будет слать запросы (сверить с документацией).
"""
from __future__ import annotations

import asyncio
import json

from .config import load_dotenv
from .providers.vilva import McpClient


async def check_vilva(url: str, key: str) -> None:
    client = McpClient(url, key)
    try:
        tools = await client.list_tools()
        print("vilva: инструменты:")
        for t in tools:
            print(f"  - {t['name']}: {json.dumps(t.get('inputSchema', {}), ensure_ascii=False)}")
        for name in ("get_credit_balance", "list_models"):
            try:
                res = await client.call_tool(name, {})
                text = "\n".join(c.get("text", "") for c in res.get("content", []))
                print(f"vilva {name}: {text[:2000]}")
            except Exception as e:
                print(f"vilva {name}: ошибка {e}")
    finally:
        await client.close()


def main() -> None:
    import os

    load_dotenv()
    if os.getenv("VILVA_API_KEY"):
        asyncio.run(check_vilva(os.getenv("VILVA_MCP_URL", "https://api.vilva.ai/mcp"), os.environ["VILVA_API_KEY"]))
    else:
        print("vilva: VILVA_API_KEY не задан")
    if os.getenv("MAGE_API_KEY"):
        base = os.getenv("MAGE_BASE_URL", "https://api.mage.space")
        print(f"mage: POST {base}{os.getenv('MAGE_SUBMIT_PATH', '/v1/generate')}, "
              f"опрос GET {base}{os.getenv('MAGE_STATUS_PATH', '/v1/requests/{id}')}")
    else:
        print("mage: MAGE_API_KEY не задан")


if __name__ == "__main__":
    main()
