from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from loguru import logger
from sqlalchemy import delete, select, update

from app.database.database import async_session
from app.database.models import GeneratedArticle, NewsCluster, Setting, Workspace
from app.services.scoring import calculate_score, transition_lifecycle


@dataclass(frozen=True)
class ClusterThresholds:
    score_threshold: int = 50
    hot_threshold: int = 80
    cluster_wait_minutes: int = 30


async def get_thresholds(
    *,
    session_factory: Callable[[], object] = async_session,
) -> ClusterThresholds:
    """Read cluster lifecycle thresholds from settings."""
    defaults = ClusterThresholds()
    values = {
        "score_threshold": defaults.score_threshold,
        "hot_threshold": defaults.hot_threshold,
        "cluster_wait_minutes": defaults.cluster_wait_minutes,
    }

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Setting).where(
                    Setting.key.in_([
                        "score_threshold",
                        "hot_threshold",
                        "cluster_wait_minutes",
                    ])
                )
            )
        ).all()

    for row in rows:
        if row.key not in values:
            continue
        try:
            values[row.key] = int(row.value)
        except (ValueError, TypeError):
            pass

    return ClusterThresholds(
        score_threshold=values["score_threshold"],
        hot_threshold=values["hot_threshold"],
        cluster_wait_minutes=values["cluster_wait_minutes"],
    )


def merge_workspace_thresholds(
    base: ClusterThresholds,
    workspace: Workspace | None,
) -> ClusterThresholds:
    """Workspace values override the global ones where set."""
    if workspace is None:
        return base
    return ClusterThresholds(
        score_threshold=(
            workspace.score_threshold
            if workspace.score_threshold is not None
            else base.score_threshold
        ),
        hot_threshold=(
            workspace.hot_threshold
            if workspace.hot_threshold is not None
            else base.hot_threshold
        ),
        cluster_wait_minutes=(
            workspace.cluster_wait_minutes
            if workspace.cluster_wait_minutes is not None
            else base.cluster_wait_minutes
        ),
    )


async def _thresholds_for_cluster(
    session: object,
    cluster: NewsCluster,
    base: ClusterThresholds,
    cache: dict[int, ClusterThresholds],
) -> ClusterThresholds:
    workspace_id = getattr(cluster, "workspace_id", None)
    if workspace_id is None:
        return base
    if workspace_id not in cache:
        workspace = await session.get(Workspace, workspace_id)
        cache[workspace_id] = merge_workspace_thresholds(base, workspace)
    return cache[workspace_id]


async def score_and_transition(
    cluster_ids: set[int],
    *,
    session_factory: Callable[[], object] = async_session,
) -> int:
    """Score clusters with AI classification and run lifecycle transitions."""
    if not cluster_ids:
        return 0

    base = await get_thresholds(session_factory=session_factory)

    async with session_factory() as session:
        clusters = (
            await session.scalars(
                select(NewsCluster).where(NewsCluster.id.in_(cluster_ids))
            )
        ).all()

        cache: dict[int, ClusterThresholds] = {}
        for cluster in clusters:
            thresholds = await _thresholds_for_cluster(session, cluster, base, cache)
            await calculate_score(
                session, cluster, run_ai_classification=True
            )
            await transition_lifecycle(
                session,
                cluster,
                score_threshold=thresholds.score_threshold,
                hot_threshold=thresholds.hot_threshold,
                cluster_wait_minutes=thresholds.cluster_wait_minutes,
            )

        await session.commit()

    return len(clusters)


async def check_waiting_clusters(
    *,
    max_cluster_age_hours: int,
    session_factory: Callable[[], object] = async_session,
) -> int:
    """Check if any waiting clusters should transition to generating."""
    base = await get_thresholds(session_factory=session_factory)
    age_cutoff = datetime.now(timezone.utc) - timedelta(hours=max_cluster_age_hours)

    async with session_factory() as session:
        clusters = (
            await session.scalars(
                select(NewsCluster).where(
                    NewsCluster.status == "waiting",
                    NewsCluster.first_seen_at >= age_cutoff,
                )
            )
        ).all()

        if not clusters:
            return 0

        cache: dict[int, ClusterThresholds] = {}
        transitioned = 0
        for cluster in clusters:
            thresholds = await _thresholds_for_cluster(session, cluster, base, cache)
            old_status = cluster.status
            await transition_lifecycle(
                session,
                cluster,
                score_threshold=thresholds.score_threshold,
                hot_threshold=thresholds.hot_threshold,
                cluster_wait_minutes=thresholds.cluster_wait_minutes,
            )
            if cluster.status != old_status:
                transitioned += 1

        if transitioned:
            await session.commit()
            logger.info(f"Waiting clusters transitioned: {transitioned}")

    return transitioned


async def check_new_clusters(
    *,
    max_cluster_age_hours: int,
    session_factory: Callable[[], object] = async_session,
) -> int:
    """Re-evaluate new clusters that meet their workspace threshold."""
    base = await get_thresholds(session_factory=session_factory)
    age_cutoff = datetime.now(timezone.utc) - timedelta(hours=max_cluster_age_hours)

    async with session_factory() as session:
        # No score filter in SQL: thresholds are per workspace, and
        # transition_lifecycle applies them per cluster.
        clusters = (
            await session.scalars(
                select(NewsCluster).where(
                    NewsCluster.status == "new",
                    NewsCluster.first_seen_at >= age_cutoff,
                )
            )
        ).all()

        if not clusters:
            return 0

        cache: dict[int, ClusterThresholds] = {}
        transitioned = 0
        for cluster in clusters:
            thresholds = await _thresholds_for_cluster(session, cluster, base, cache)
            old_status = cluster.status
            await transition_lifecycle(
                session,
                cluster,
                score_threshold=thresholds.score_threshold,
                hot_threshold=thresholds.hot_threshold,
                cluster_wait_minutes=thresholds.cluster_wait_minutes,
            )
            if cluster.status != old_status:
                transitioned += 1

        if transitioned:
            await session.commit()
            logger.info(f"New clusters promoted: {transitioned}")

    return transitioned


async def expire_old_clusters(
    *,
    max_cluster_age_hours: int,
    session_factory: Callable[[], object] = async_session,
) -> int:
    """Expire old new/waiting clusters so they stop being re-evaluated.

    Clusters that already have generated draft versions are marked 'rejected'
    (cleanup retention applies) instead of deleted — deleting would CASCADE
    into generated_articles and destroy moderator-visible drafts.
    """
    now = datetime.now(timezone.utc)
    age_cutoff = now - timedelta(hours=max_cluster_age_hours)

    async with session_factory() as session:
        expired = (
            NewsCluster.status.in_(["new", "waiting"]),
            NewsCluster.first_seen_at < age_cutoff,
        )
        has_drafts = (
            select(GeneratedArticle.id)
            .where(GeneratedArticle.cluster_id == NewsCluster.id)
            .exists()
        )

        rejected_result = await session.execute(
            update(NewsCluster)
            .where(*expired, has_drafts)
            .values(status="rejected", status_changed_at=now)
            .execution_options(synchronize_session=False)
        )
        deleted_result = await session.execute(
            delete(NewsCluster)
            .where(*expired, ~has_drafts)
            .execution_options(synchronize_session=False)
        )
        rejected_count = rejected_result.rowcount or 0
        deleted_count = deleted_result.rowcount or 0

        if not rejected_count and not deleted_count:
            return 0

        await session.commit()
        if rejected_count:
            logger.info(
                f"Rejected {rejected_count} old drafted cluster(s) older than "
                f"{max_cluster_age_hours}h (drafts preserved)"
            )
        if deleted_count:
            logger.info(
                f"Deleted {deleted_count} old cluster(s) older than "
                f"{max_cluster_age_hours}h"
            )

    return rejected_count + deleted_count


async def recover_stuck_clusters(
    *,
    generating_timeout_minutes: int,
    session_factory: Callable[[], object] = async_session,
) -> int:
    """Revert clusters stuck in generating beyond the timeout back to waiting."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=generating_timeout_minutes
    )

    async with session_factory() as session:
        clusters = (
            await session.scalars(
                select(NewsCluster).where(
                    NewsCluster.status == "generating",
                    NewsCluster.status_changed_at < cutoff,
                )
            )
        ).all()

        if not clusters:
            return 0

        now = datetime.now(timezone.utc)
        for cluster in clusters:
            cluster.status = "waiting"
            cluster.status_changed_at = now
            logger.warning(
                f"Cluster #{cluster.id} stuck in 'generating' for "
                f">{generating_timeout_minutes} min - reverted to 'waiting'"
            )

        await session.commit()
        logger.info(f"Recovered {len(clusters)} stuck cluster(s)")

    return len(clusters)
