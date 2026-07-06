#!/usr/bin/env python3
"""Create and send a moderation draft for Telegram smoke testing."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _smoke_external_id(now: datetime) -> str:
    return f"local-smoke-{int(now.timestamp())}"


async def create_and_send_test_draft() -> int:
    from aiogram import Bot
    from sqlalchemy import select

    from app.config import settings
    from app.database.database import async_session, close
    from app.database.models import (
        DEFAULT_WORKSPACE_ID,
        NewsCluster,
        RawArticle,
        Source,
    )
    from app.services.content_generator import generate_article
    from app.services.generation_pipeline import record_delivery_result
    from app.services.notifier import send_to_moderators, set_bot

    now = datetime.now(timezone.utc)
    bot = Bot(token=settings.bot_token)
    set_bot(bot)

    try:
        async with async_session() as session:
            source = await session.scalar(
                select(Source).where(Source.url == "https://local.test/ai-news-smoke")
            )
            if not source:
                source = Source(
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    name="Local Smoke Source",
                    url="https://local.test/ai-news-smoke",
                    type="rss",
                    categories=["general"],
                    weight=10,
                    active=True,
                )
                session.add(source)
                await session.flush()

            cluster = NewsCluster(
                workspace_id=DEFAULT_WORKSPACE_ID,
                topic="Local Telegram smoke test",
                topic_original="Local Telegram smoke test",
                category="general",
                news_type="other",
                score=90,
                is_hot=False,
                status="generating",
                sources_count=1,
                first_seen_at=now,
            )
            session.add(cluster)
            await session.flush()

            session.add(
                RawArticle(
                    source_id=source.id,
                    external_id=_smoke_external_id(now),
                    title="Local Telegram smoke test",
                    content=(
                        "A controlled local article used to verify Telegram "
                        "moderation buttons without external AI API calls."
                    ),
                    url="https://local.test/ai-news-smoke/article",
                    language="en",
                    published_at=now,
                    fetched_at=now,
                    cluster_id=cluster.id,
                )
            )
            await session.flush()

            article = await generate_article(session, cluster)
            if not article:
                raise RuntimeError("local test draft generation returned no article")

            await session.commit()
            delivery = await send_to_moderators(session, article, cluster)
            await session.commit()
            record_delivery_result(article, cluster, delivery)

            print(
                "created local test draft "
                f"article_id={article.id} cluster_id={cluster.id} "
                f"delivered={delivery.delivered_count}/{delivery.total_recipients}"
            )
            return article.id
    finally:
        await bot.session.close()
        await close()


def main() -> int:
    asyncio.run(create_and_send_test_draft())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
