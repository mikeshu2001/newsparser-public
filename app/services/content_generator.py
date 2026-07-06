"""Generate news articles from clusters using AI providers.

Pipeline: collect cluster sources → build prompt → call AI → parse response → save.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    GeneratedArticle,
    NewsCluster,
    RawArticle,
    Setting,
    Source,
    Workspace,
)
from app.services import ai_providers

_DEFAULT_PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "prompts" / "default_news_prompt.txt"
_DEFAULT_PROMPT: str = ""
if _DEFAULT_PROMPT_PATH.exists():
    _DEFAULT_PROMPT = _DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_prompt(
    cluster: NewsCluster,
    articles: list[RawArticle],
    source_map: dict[int, Source],
    prompt_template: str,
    tone_of_voice: str,
    edit_comment: Optional[str] = None,
) -> str:
    """Fill prompt template with cluster data."""
    sources_block_parts: list[str] = []
    for i, article in enumerate(articles, 1):
        src = source_map.get(article.source_id)
        src_name = src.name if src else "Unknown"
        content = (article.content or "")[:2000]
        url_line = f"\nСсылка: {article.url}" if article.url else ""
        sources_block_parts.append(
            f"Источник {i} ({src_name}):\n"
            f"Заголовок: {article.title}\n"
            f"{content}"
            f"{url_line}"
        )

    sources_block = "\n\n".join(sources_block_parts)

    # str.replace instead of str.format: the template is admin-editable and may
    # contain literal braces (JSON examples), which .format would crash on.
    replacements = {
        "{tone_of_voice}": tone_of_voice or "Нейтральный, информативный стиль.",
        "{category}": cluster.category or "general",
        "{news_type}": cluster.news_type or "other",
        "{sources_block}": sources_block,
    }
    prompt = prompt_template
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)

    if edit_comment:
        prompt += f"\n\nДополнительные правки от редактора: {edit_comment}"

    return prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_HEADLINE_RE = re.compile(r"ЗАГОЛОВОК:\s*(.+?)(?=\nТЕКСТ:)", re.DOTALL)
_BODY_RE = re.compile(r"ТЕКСТ:\s*(.+?)(?=\nОПИСАНИЕ:|\Z)", re.DOTALL)
_SUMMARY_RE = re.compile(r"ОПИСАНИЕ:\s*(.+)", re.DOTALL)


def _parse_response(response: str) -> dict[str, str]:
    """Extract headline / body from AI response."""
    headline_m = _HEADLINE_RE.search(response)
    body_m = _BODY_RE.search(response)
    summary_m = _SUMMARY_RE.search(response)

    if headline_m and body_m:
        summary = summary_m.group(1).strip()[:280] if summary_m else ""
        return {
            "headline": headline_m.group(1).strip(),
            "body": body_m.group(1).strip(),
            "summary": summary,
        }

    # Fallback: at least try to get the headline
    if headline_m:
        return {
            "headline": headline_m.group(1).strip(),
            "body": response.strip(),
            "summary": "",
        }

    # Last resort: first line
    lines = response.strip().split("\n")
    return {
        "headline": lines[0][:100] if lines else "Без заголовка",
        "body": response.strip(),
        "summary": "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_article(
    session: AsyncSession,
    cluster: NewsCluster,
    edit_comment: Optional[str] = None,
) -> Optional[GeneratedArticle]:
    """Generate (or re-generate) an article for *cluster*.

    Returns the saved GeneratedArticle or None on failure.
    """
    # 1. Collect raw articles
    articles = (
        await session.scalars(
            select(RawArticle).where(RawArticle.cluster_id == cluster.id)
        )
    ).all()

    if not articles:
        logger.warning(f"Cluster #{cluster.id} has no articles, skipping generation")
        return None

    # 2. Fetch sources for those articles
    source_ids = list({a.source_id for a in articles})
    sources = (
        await session.scalars(select(Source).where(Source.id.in_(source_ids)))
    ).all()
    source_map = {s.id: s for s in sources}

    # 3. Prompt/tone resolution: workspace -> global settings -> default file
    workspace = await session.get(Workspace, cluster.workspace_id)

    prompt_template = ""
    tone_of_voice = ""
    rows = (
        await session.scalars(
            select(Setting).where(Setting.key.in_(["news_prompt", "tone_of_voice"]))
        )
    ).all()
    for row in rows:
        if row.key == "news_prompt":
            prompt_template = row.value
        elif row.key == "tone_of_voice":
            tone_of_voice = row.value

    if workspace is not None:
        if workspace.news_prompt and "{sources_block}" in workspace.news_prompt:
            prompt_template = workspace.news_prompt
        if workspace.tone_of_voice:
            tone_of_voice = workspace.tone_of_voice

    # Validate: prompt must contain {sources_block} to be usable
    if not prompt_template or "{sources_block}" not in prompt_template:
        if _DEFAULT_PROMPT:
            logger.warning("DB prompt missing or invalid — using default from file")
            prompt_template = _DEFAULT_PROMPT
        else:
            logger.error("No usable prompt — cannot generate")
            return None

    # 4. Build prompt
    prompt = _build_prompt(
        cluster, list(articles), source_map,
        prompt_template, tone_of_voice, edit_comment,
    )

    # 5. Call AI (workspace key when configured — BYOK)
    try:
        response_text, provider_name = await ai_providers.generate(
            prompt, workspace=workspace
        )
    except ai_providers.AllProvidersFailedError as e:
        logger.error(f"Generation failed for cluster #{cluster.id}: {e}")
        return None

    # 6. Parse response
    parsed = _parse_response(response_text)

    # 7. Determine version
    latest_version = 0
    existing = (
        await session.scalars(
            select(GeneratedArticle)
            .where(GeneratedArticle.cluster_id == cluster.id)
            .order_by(GeneratedArticle.version.desc())
        )
    ).first()
    if existing:
        latest_version = existing.version

    # 8. Save
    article = GeneratedArticle(
        cluster_id=cluster.id,
        headline=parsed["headline"],
        body=parsed["body"],
        summary=parsed["summary"],
        ai_provider=provider_name,
        version=latest_version + 1,
        edit_comment=edit_comment,
    )
    session.add(article)

    # Transition cluster → pending_review
    cluster.status = "pending_review"
    cluster.status_changed_at = datetime.now(timezone.utc)

    await session.flush()

    logger.info(
        f"Generated article #{article.id} v{article.version} "
        f"for cluster #{cluster.id} via {provider_name}"
    )
    return article
