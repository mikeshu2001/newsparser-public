from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import select

from app.database.database import async_session
from app.database.models import GeneratedArticle, NewsCluster, Workspace
from app.services.content_generator import generate_article
from app.services.notifier import send_to_moderators

_manual_generation_claims: set[int] = set()


def mark_manual_generation(cluster_id: int) -> None:
    _manual_generation_claims.add(cluster_id)


def unmark_manual_generation(cluster_id: int) -> None:
    _manual_generation_claims.discard(cluster_id)


def is_manual_generation_claimed(cluster_id: int) -> bool:
    return cluster_id in _manual_generation_claims


def record_delivery_result(
    article: GeneratedArticle,
    cluster: NewsCluster,
    delivery: object,
) -> bool:
    """Return True when at least one moderator notification was delivered."""
    if getattr(delivery, "any_delivered", False):
        return True

    logger.error(
        f"Generated article #{article.id} for cluster #{cluster.id} "
        "was not delivered to any moderator/admin; "
        "it remains visible in /queue as pending_review"
    )
    return False


async def generate_and_notify(
    *,
    session_factory: Callable[[], object] = async_session,
    max_cluster_age_hours: int = 24,
) -> tuple[int, int]:
    """Generate articles for clusters in 'generating' status and notify moderators.

    Returns (delivered_count, undelivered_count).
    """
    age_cutoff = datetime.now(timezone.utc) - timedelta(hours=max_cluster_age_hours)

    async with session_factory() as session:
        clusters = (
            await session.scalars(
                select(NewsCluster).where(
                    NewsCluster.status == "generating",
                    NewsCluster.first_seen_at >= age_cutoff,
                )
            )
        ).all()

        if not clusters:
            return 0, 0

        cluster_ids = [cluster.id for cluster in clusters]

        generated_count = 0
        undelivered_count = 0
        for cluster_id in cluster_ids:
            if is_manual_generation_claimed(cluster_id):
                logger.info(
                    f"Cluster #{cluster_id} is being regenerated manually; "
                    "scheduler generation skipped"
                )
                continue
            try:
                # Re-fetch per iteration: rollback from a previous iteration
                # expires loaded instances, and touching an expired attribute
                # raises MissingGreenlet under the async session.
                cluster = await session.get(NewsCluster, cluster_id)
                if cluster is None or cluster.status != "generating":
                    continue
                article = await generate_article(session, cluster)
                if article:
                    await session.commit()
                    workspace = (
                        await session.get(Workspace, cluster.workspace_id)
                        if cluster.workspace_id
                        else None
                    )
                    delivery = await send_to_moderators(
                        session, article, cluster, workspace
                    )
                    await session.commit()
                    if record_delivery_result(article, cluster, delivery):
                        generated_count += 1
                    else:
                        undelivered_count += 1
                else:
                    logger.warning(
                        f"Cluster #{cluster_id}: generation returned None, "
                        f"reverting to 'waiting' for retry"
                    )
                    cluster.status = "waiting"
                    cluster.status_changed_at = datetime.now(timezone.utc)
                    await session.commit()
            except Exception as e:
                logger.error(f"Generation/notify failed for cluster #{cluster_id}: {e}")
                await session.rollback()

        if generated_count:
            logger.info(f"Generated and notified: {generated_count} articles")
        if undelivered_count:
            logger.error(
                f"Generated but not delivered to moderators: {undelivered_count} articles"
            )

    return generated_count, undelivered_count
