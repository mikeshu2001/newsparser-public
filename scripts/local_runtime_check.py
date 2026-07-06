#!/usr/bin/env python3
"""Local Telegram smoke-test readiness checks without printing secrets."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _validation_error_lines(error: Exception) -> list[str]:
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [f"{type(error).__name__}: {error}"]

    lines: list[str] = []
    for item in errors():
        loc = ".".join(str(part) for part in item.get("loc", ())) or "settings"
        msg = item.get("msg", "invalid")
        lines.append(f"{loc}: {msg}")
    return lines


def _print_status(name: str, ok: bool, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"{'OK' if ok else 'FAIL'} {name}{suffix}")


def _load_settings() -> Any | None:
    try:
        from app.config import settings
    except Exception as e:
        _print_status("settings", False)
        for line in _validation_error_lines(e):
            print(f"  {line}")
        return None

    _print_status("settings", True)
    return settings


def _configured_provider_names() -> list[str]:
    from app.services import ai_providers

    return [
        provider.name
        for provider in ai_providers._providers
        if provider.is_configured()
    ]


async def _check_postgres(*, prepare_db: bool) -> bool:
    try:
        if prepare_db:
            from app.database.database import close, ensure_schema, seed_data

            await ensure_schema()
            await seed_data()
            await close()
        else:
            from sqlalchemy import text

            from app.database.database import close, engine

            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
            await close()
    except Exception as e:
        _print_status("postgres", False, type(e).__name__)
        return False

    _print_status("postgres", True, "schema ready" if prepare_db else "reachable")
    return True


async def _check_redis() -> bool:
    try:
        from redis.asyncio import Redis

        from app.config import settings

        client = Redis.from_url(settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()
    except Exception as e:
        _print_status("redis", False, type(e).__name__)
        return False

    _print_status("redis", True, "reachable")
    return True


async def _check_telegram() -> bool:
    try:
        from aiogram import Bot

        from app.config import settings

        bot = Bot(token=settings.bot_token)
        try:
            me = await bot.get_me()
        finally:
            await bot.session.close()
    except Exception as e:
        _print_status("telegram", False, type(e).__name__)
        return False

    username = f"@{me.username}" if me.username else str(me.id)
    _print_status("telegram", True, username)
    return True


async def _check_ai(dry_run: bool) -> bool:
    try:
        from app.config import settings
        from app.services import ai_providers

        provider_names = _configured_provider_names()
        if not provider_names:
            _print_status("ai providers", False, "none configured")
            return False

        _print_status("ai providers", True, ", ".join(provider_names))

        if not dry_run:
            return True

        if not settings.local_ai_provider_enabled:
            # --check-ai promises a no-cost dry run; without the local stub
            # the provider chain would hit real paid backends.
            _print_status(
                "ai dry run",
                True,
                "skipped: LOCAL_AI_PROVIDER_ENABLED is not true",
            )
            return True

        text, provider_name = await ai_providers.generate(
            "Ответь строго в формате JSON: local smoke test",
            use_scoring_model=True,
            max_tokens=64,
            temperature=0.0,
        )
    except Exception as e:
        _print_status("ai dry run", False, type(e).__name__)
        return False

    _print_status("ai dry run", bool(text), provider_name)
    return bool(text)


async def run_checks(args: argparse.Namespace) -> int:
    settings = _load_settings()
    if settings is None:
        return 1

    results = [
        await _check_postgres(prepare_db=args.prepare_db),
        await _check_redis(),
        await _check_ai(args.check_ai),
    ]
    if args.telegram:
        results.append(await _check_telegram())

    return 0 if all(results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-db",
        action="store_true",
        help="run migrations and seed data before local bot startup",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="check Telegram Bot API getMe with BOT_TOKEN",
    )
    parser.add_argument(
        "--check-ai",
        action="store_true",
        help="run a no-cost AI dry run only when LOCAL_AI_PROVIDER_ENABLED=true",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run_checks(args))


if __name__ == "__main__":
    raise SystemExit(main())
