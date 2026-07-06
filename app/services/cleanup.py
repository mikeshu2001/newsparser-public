from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, or_, select

from app.database.database import async_session
from app.database.models import NewsCluster, RawArticle


async def cleanup_old_data(
    *,
    session_factory: Callable[[], object] = async_session,
) -> tuple[int, int, int]:
    """Delete old raw articles and old inactive clusters.

    Returns (deleted_raw, deleted_rejected, deleted_approved).
    """
    async with session_factory() as session:
        now = datetime.now(timezone.utc)

        active_statuses = {"waiting", "generating", "pending_review"}
        active_cluster_ids = select(NewsCluster.id).where(
            NewsCluster.status.in_(active_statuses)
        ).scalar_subquery()

        cutoff_raw = now - timedelta(days=30)
        result = await session.execute(
            delete(RawArticle).where(
                RawArticle.fetched_at < cutoff_raw,
                or_(
                    RawArticle.cluster_id.is_(None),
                    RawArticle.cluster_id.notin_(active_cluster_ids),
                ),
            )
        )
        deleted_raw = result.rowcount

        cutoff_rejected = now - timedelta(days=7)
        result = await session.execute(
            delete(NewsCluster).where(
                NewsCluster.status == "rejected",
                NewsCluster.updated_at < cutoff_rejected,
            )
        )
        deleted_rejected = result.rowcount

        cutoff_approved = now - timedelta(days=90)
        result = await session.execute(
            delete(NewsCluster).where(
                NewsCluster.status == "approved",
                NewsCluster.updated_at < cutoff_approved,
            )
        )
        deleted_approved = result.rowcount

        await session.commit()

    return deleted_raw, deleted_rejected, deleted_approved
