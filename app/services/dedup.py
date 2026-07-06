"""Deduplication & clustering service.

For each new raw_article, finds an existing cluster (by fuzzy title match or
cross-language entity overlap) or creates a new one.

Primary window: clusters from the last 48 hours with status
'new', 'waiting', 'generating', or 'pending_review'.

Secondary check: if no active cluster matches, look at 'approved' clusters
from the last 24 hours — if matched, return None (article already covered).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NewsCluster, RawArticle
from app.utils.text import GENERIC_TERMS, extract_entities, normalize_title

# Thresholds
FUZZY_THRESHOLD = 75  # token_sort_ratio >= 75% → same cluster
# A single shared proper-noun entity is too weak on its own ('openai' would
# merge every story about the brand); it must be corroborated by moderate
# title similarity. Two or more shared entities match as before.
ENTITY_CORROBORATION_FUZZY = 55
DEDUP_WINDOW_HOURS = 48
APPROVED_COOLDOWN_HOURS = 24


def _match_score(
    norm_title: str,
    article_entities: set[str],
    cluster: NewsCluster,
) -> float:
    """Return match strength between article and cluster; 0 means no match."""
    ratio = fuzz.token_sort_ratio(norm_title, cluster.topic) if cluster.topic else 0.0
    if ratio >= FUZZY_THRESHOLD:
        return ratio

    if article_entities:
        cluster_entities = _cluster_entities(cluster)
        shared = article_entities & cluster_entities
        if len(shared) >= 2:
            return len(shared) * 20  # synthetic score
        if shared:
            has_proper_noun = any(e not in GENERIC_TERMS for e in shared)
            if has_proper_noun and ratio >= ENTITY_CORROBORATION_FUZZY:
                return len(shared) * 20

    return 0.0


async def deduplicate(
    session: AsyncSession,
    article: RawArticle,
    workspace_id: int,
) -> Optional[int]:
    """Assign article to a cluster (existing or new) within one workspace.

    Returns cluster_id if article should be processed further.
    Returns None if article duplicates an already-approved topic (last 24h).
    """
    norm_title = normalize_title(article.title)
    article_entities = extract_entities(article.title)

    # --- Primary window: active clusters (new/waiting/generating/pending_review) ---
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)
    clusters = (
        await session.scalars(
            select(NewsCluster).where(
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status.in_(["new", "waiting", "generating", "pending_review"]),
                NewsCluster.created_at >= cutoff,
            )
        )
    ).all()

    best_cluster: Optional[NewsCluster] = None
    best_score: float = 0

    for cluster in clusters:
        score = _match_score(norm_title, article_entities, cluster)
        if score > best_score:
            best_score = score
            best_cluster = cluster

    if best_cluster:
        # Add to existing cluster
        article.cluster_id = best_cluster.id
        best_cluster.updated_at = datetime.now(timezone.utc)
        logger.debug(
            f"Article '{article.title[:60]}' → existing cluster #{best_cluster.id} "
            f"(score={best_score:.0f})"
        )
        return best_cluster.id

    # --- Secondary check: approved clusters within last 24h ---
    approved_cutoff = datetime.now(timezone.utc) - timedelta(hours=APPROVED_COOLDOWN_HOURS)
    approved_clusters = (
        await session.scalars(
            select(NewsCluster).where(
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status == "approved",
                NewsCluster.created_at >= approved_cutoff,
            )
        )
    ).all()

    for cluster in approved_clusters:
        if _match_score(norm_title, article_entities, cluster) > 0:
            logger.debug(
                f"Article '{article.title[:60]}' → already covered by approved "
                f"cluster #{cluster.id}, skipping"
            )
            return None

    # Create new cluster
    cluster = NewsCluster(
        workspace_id=workspace_id,
        topic=norm_title,
        topic_original=article.title,
        status="new",
        sources_count=1,
        first_seen_at=datetime.now(timezone.utc),
    )
    session.add(cluster)
    await session.flush()  # get cluster.id
    article.cluster_id = cluster.id
    logger.debug(
        f"Article '{article.title[:60]}' → new cluster #{cluster.id}"
    )
    return cluster.id


def _cluster_entities(cluster: NewsCluster) -> set[str]:
    """Extract entities from the cluster's original (non-normalized) title.

    Using topic_original because normalized topic strips punctuation
    (e.g. 'GPT-4' → 'gpt 4'), breaking regex patterns like GPT-\\d\\S*.
    Falls back to normalized topic for clusters created before this field existed.
    """
    if cluster.topic_original:
        return extract_entities(cluster.topic_original)
    if not cluster.topic:
        return set()
    return extract_entities(cluster.topic)
