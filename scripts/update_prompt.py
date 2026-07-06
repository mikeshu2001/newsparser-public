#!/usr/bin/env python3
"""Update news_prompt in the database from prompts/default_news_prompt.txt.

Usage (from project root, with venv activated):
    python scripts/update_prompt.py
"""

import asyncio
import sys
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "default_news_prompt.txt"
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


async def main() -> int:
    from sqlalchemy import select
    from app.database.database import async_session, engine
    from app.database.models import Setting

    if not PROMPT_PATH.exists():
        print(f"ERROR: prompt file not found: {PROMPT_PATH}")
        return 1

    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    print(f"Read prompt: {len(prompt)} chars from {PROMPT_PATH.name}")

    async with async_session() as session:
        row = await session.get(Setting, "news_prompt")
        if row:
            row.value = prompt
            print("Updated existing news_prompt in DB")
        else:
            session.add(Setting(key="news_prompt", value=prompt))
            print("Inserted new news_prompt into DB")
        await session.commit()

    await engine.dispose()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
