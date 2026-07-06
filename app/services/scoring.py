"""Rule-based + AI scoring & burst detection for news clusters.

Score formula:
  source_weight  = min(best_source.weight * 3, 30)
  cross_refs     = (distinct source count - 1) * 15
  burst          = +30 if 3+ articles in 3 hours
  triggers       = +10 if trigger phrases found
  first_source   = +15 if best source weight >= 9
  staleness      = -max(0, hours_old - 6) * 5
  ai_type_bonus  = +5..+30 based on AI news-type classification

  total = min(100, sum of above)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import NewsCluster, RawArticle, Source, Workspace
from app.utils.text import has_trigger_phrases

# Burst detection thresholds
BURST_ARTICLE_COUNT = 3
BURST_WINDOW_HOURS = 3

# AI type → bonus points
NEWS_TYPE_BONUS: dict[str, int] = {
    "product_launch": 30,
    "major_update": 25,
    "breakthrough": 25,
    "partnership": 20,
    "funding": 15,
    "opinion": 10,
    "tutorial": 5,
    "other": 5,
}

_CLASSIFY_PROMPT = (
    "Определи тип новости и её важность для AI-индустрии. "
    "Ответь строго в формате JSON:\n"
    '{{"type": "product_launch|major_update|partnership|breakthrough|funding|opinion|tutorial|other", '
    '"importance": 1-10, '
    '"category": "llm|image_gen|video_gen|audio_gen|code|robotics|chips|research|general"}}\n\n'
    "Новость: {title}\n{content}"
)


async def classify_news_type(
    title: str,
    content: str,
    workspace: Optional[Workspace] = None,
) -> tuple[Optional[str], Optional[str], int]:
    """Use a cheap AI model to classify news type & category.

    Returns (news_type, category, bonus_points).
    Falls back to (None, None, 0) on any error.
    """
    from app.services.ai_providers import AllProvidersFailedError, generate

    prompt = _CLASSIFY_PROMPT.format(
        title=title,
        content=(content or "")[:300],
    )
    if workspace is not None and workspace.topic:
        prompt = f"Тематика издания: {workspace.topic}\n\n{prompt}"

    try:
        response, _ = await generate(
            prompt,
            use_scoring_model=True,
            max_tokens=256,
            temperature=0.0,
            workspace=workspace,
        )
    except AllProvidersFailedError:
        logger.warning("AI classification failed — all providers down")
        return None, None, 0

    # Parse JSON from response
    try:
        text = response.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        data = json.loads(text)
        news_type = data.get("type", "other")
        category = data.get("category", "general")
        bonus = NEWS_TYPE_BONUS.get(news_type, 5)
        return news_type, category, bonus
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"Failed to parse AI classification response: {e}")
        return None, None, 0


async def calculate_score(
    session: AsyncSession,
    cluster: NewsCluster,
    *,
    run_ai_classification: bool = False,
) -> int:
    """Calculate score for a cluster. Updates cluster in-place.

    When *run_ai_classification* is True, calls AI to classify news type
    and adds the type bonus to the score.
    """
    # Fetch articles belonging to this cluster with their sources
    articles = (
        await session.scalars(
            select(RawArticle).where(RawArticle.cluster_id == cluster.id)
        )
    ).all()

    if not articles:
        cluster.score = 0
        return 0

    # Fetch source weights for all articles
    source_ids = list({a.source_id for a in articles})
    distinct_source_count = len(source_ids)
    cluster.sources_count = max(1, distinct_source_count)
    sources = (
        await session.scalars(select(Source).where(Source.id.in_(source_ids)))
    ).all()
    source_map = {s.id: s for s in sources}

    # --- 1. Source weight: best source in cluster ---
    best_weight = max(
        (source_map[a.source_id].weight for a in articles if a.source_id in source_map),
        default=5,
    )
    weight_score = min(best_weight * 3, 30)

    # --- 2. Cross-references ---
    cross_ref_score = (distinct_source_count - 1) * 15

    # --- 3. Burst detection ---
    burst_score = 0
    is_hot_burst = False
    now = datetime.now(timezone.utc)
    burst_cutoff = now - timedelta(hours=BURST_WINDOW_HOURS)
    recent_count = sum(
        1 for a in articles
        if a.fetched_at and a.fetched_at >= burst_cutoff
    )
    if recent_count >= BURST_ARTICLE_COUNT:
        burst_score = 30
        is_hot_burst = True

    # --- 4. Trigger phrases ---
    trigger_score = 0
    for a in articles:
        if has_trigger_phrases(a.title):
            trigger_score = 10
            break

    # --- 5. First-source bonus (official blog, weight >= 9) ---
    first_source_score = 0
    if best_weight >= 9:
        first_source_score = 15

    # --- 6. Staleness penalty ---
    hours_old = (now - cluster.first_seen_at).total_seconds() / 3600
    staleness_penalty = max(0, hours_old - 6) * 5

    # --- 7. AI type bonus ---
    ai_type_bonus = 0
    if run_ai_classification and cluster.news_type is None:
        workspace = await session.get(Workspace, cluster.workspace_id)
        first_article = articles[0]
        news_type, category, bonus = await classify_news_type(
            first_article.title, first_article.content, workspace
        )
        if news_type:
            cluster.news_type = news_type
            ai_type_bonus = bonus
        if category:
            cluster.category = category
    elif cluster.news_type:
        ai_type_bonus = NEWS_TYPE_BONUS.get(cluster.news_type, 5)

    # --- Total ---
    total = int(
        weight_score
        + cross_ref_score
        + burst_score
        + trigger_score
        + first_source_score
        + ai_type_bonus
        - staleness_penalty
    )
    total = max(0, min(100, total))

    cluster.score = total
    cluster.is_hot = is_hot_burst or total >= 80
    cluster.updated_at = now

    logger.debug(
        f"Cluster #{cluster.id} score={total} "
        f"(weight={weight_score}, xref={cross_ref_score}, burst={burst_score}, "
        f"trigger={trigger_score}, first={first_source_score}, "
        f"stale=-{staleness_penalty:.0f}, ai={ai_type_bonus})"
    )

    return total


async def transition_lifecycle(
    session: AsyncSession,
    cluster: NewsCluster,
    score_threshold: int = 50,
    hot_threshold: int = 80,
    cluster_wait_minutes: int = 30,
) -> None:
    """Transition cluster through lifecycle states based on score.

    new → waiting  (score >= threshold)
    waiting → generating  (after cluster_wait_minutes OR immediately if HOT)
    """
    now = datetime.now(timezone.utc)

    if cluster.status == "new" and cluster.score >= score_threshold:
        if cluster.score >= hot_threshold:
            # HOT — skip waiting, go straight to generating
            cluster.status = "generating"
            cluster.is_hot = True
            cluster.updated_at = now
            cluster.status_changed_at = now
            logger.info(
                f"Cluster #{cluster.id} HOT (score={cluster.score}) → generating"
            )
        else:
            cluster.status = "waiting"
            cluster.updated_at = now
            cluster.status_changed_at = now
            logger.info(
                f"Cluster #{cluster.id} (score={cluster.score}) → waiting"
            )

    elif cluster.status == "waiting":
        # Measured from the moment the cluster entered 'waiting' — updated_at
        # is unusable here because dedup/scoring bump it on every new article.
        waited = (now - cluster.status_changed_at).total_seconds() / 60
        if waited >= cluster_wait_minutes or cluster.score >= hot_threshold:
            cluster.status = "generating"
            cluster.updated_at = now
            cluster.status_changed_at = now
            logger.info(
                f"Cluster #{cluster.id} (waited {waited:.0f}min) → generating"
            )
