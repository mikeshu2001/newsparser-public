from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database.migrations import run_migrations
from app.database.models import DEFAULT_WORKSPACE_ID, BotUser, Setting, Source

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def ensure_schema() -> None:
    await run_migrations(engine)
    logger.info("Database schema ready")


async def seed_data() -> None:
    async with async_session() as session:
        await _seed_admin_users(session)
        await _seed_settings(session)
        await _seed_sources(session)
        await session.commit()
    logger.info("Seed data applied")


async def _seed_admin_users(session: AsyncSession) -> None:
    for user_id in settings.admin_user_ids:
        existing = await session.get(BotUser, user_id)
        if existing:
            if existing.role != "admin":
                existing.role = "admin"
                logger.warning(f"Restored bootstrap admin role for user: {user_id}")
            if not existing.is_active:
                existing.is_active = True
                logger.warning(f"Reactivated bootstrap admin user: {user_id}")
        else:
            session.add(BotUser(id=user_id, role="admin", is_active=True))
            logger.info(f"Seeded admin user: {user_id}")


async def _seed_settings(session: AsyncSession) -> None:
    prompt_path = Path("prompts/default_news_prompt.txt")
    default_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    defaults = {
        "score_threshold": "50",
        "hot_threshold": "80",
        "parsing_interval_minutes": "15",
        "cluster_wait_minutes": "30",
        "news_prompt": default_prompt,
        "tone_of_voice": "",
    }
    for key, value in defaults.items():
        existing = await session.get(Setting, key)
        if not existing:
            session.add(Setting(key=key, value=value))


SEED_SOURCES = [
    # LLM
    {
        "name": "OpenAI Blog",
        "url": "https://openai.com/news/rss.xml",
        "type": "rss",
        "categories": ["llm"],
        "weight": 10,
    },
    {
        "name": "Anthropic Blog",
        "url": "https://www.anthropic.com/news",
        "type": "web",
        "categories": ["llm"],
        "weight": 10,
        "scraper_config": {
            "list_url": "https://www.anthropic.com/news",
            "article_selector": "a[href*='/news/']",
            "fetch_meta": True,
        },
    },
    {
        "name": "Google DeepMind",
        "url": "https://deepmind.google/blog/rss.xml",
        "type": "rss",
        "categories": ["llm", "research"],
        "weight": 10,
    },
    # Meta AI — no RSS, no sitemap, web scraping returns 400.
    # Temporarily disabled; re-add when a reliable feed is found.
    # Pure-AI vendor blogs get weight >= 9: every post is relevant, and the
    # keyword filter would otherwise drop titles without AI terms
    # ("Bringing more control over your connectors").
    {
        "name": "Mistral AI",
        "url": "https://mistral.ai/rss.xml",
        "type": "rss",
        "categories": ["llm"],
        "weight": 9,
    },
    {
        "name": "Google Gemini Blog",
        "url": "https://blog.google/products/gemini/rss/",
        "type": "rss",
        "categories": ["llm", "general"],
        "weight": 9,
    },
    {
        "name": "Qwen Blog",
        "url": "https://qwenlm.github.io/blog/index.xml",
        "type": "rss",
        "categories": ["llm"],
        "weight": 9,
    },
    {
        "name": "Hugging Face",
        "url": "https://huggingface.co/blog/feed.xml",
        "type": "rss",
        "categories": ["llm", "research"],
        "weight": 7,
    },
    # Image Gen
    # Stability has no RSS; their sitemap carries lastmod for /news-updates/.
    {
        "name": "Stability AI",
        "url": "https://stability.ai/sitemap.xml",
        "type": "sitemap",
        "categories": ["image_gen"],
        "weight": 9,
        "scraper_config": {
            "sitemap_url": "https://stability.ai/sitemap.xml",
            "url_contains": "/news-updates/",
        },
    },
    # Media
    {
        "name": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
        "type": "rss",
        "categories": ["general"],
        "weight": 7,
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "type": "rss",
        "categories": ["general"],
        "weight": 7,
    },
    {
        "name": "The Verge AI",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence",
        "type": "rss",
        "categories": ["general"],
        "weight": 6,
    },
    {
        "name": "Ars Technica AI",
        "url": "https://arstechnica.com/ai/feed/",
        "type": "rss",
        "categories": ["general"],
        "weight": 6,
    },
    {
        "name": "Habr ML",
        "url": "https://habr.com/ru/rss/hubs/machine_learning/articles/all/",
        "type": "rss",
        "categories": ["general"],
        "weight": 6,
    },
    {
        "name": "Habr AI",
        "url": "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/all/",
        "type": "rss",
        "categories": ["general"],
        "weight": 6,
    },
    # Code
    {
        "name": "GitHub Blog",
        "url": "https://github.blog/feed/",
        "type": "rss",
        "categories": ["code"],
        "weight": 8,
    },
    # Fast AI media (editorial, covering events within hours)
    {
        "name": "The Decoder",
        "url": "https://the-decoder.com/feed/",
        "type": "rss",
        "categories": ["general", "llm", "image_gen", "video_gen"],
        "weight": 8,
    },
    {
        "name": "MarkTechPost",
        "url": "https://www.marktechpost.com/feed",
        "type": "rss",
        "categories": ["general", "llm", "research"],
        "weight": 7,
    },
    {
        "name": "AI News",
        "url": "https://www.artificialintelligence-news.com/feed/rss/",
        "type": "rss",
        "categories": ["general", "llm"],
        "weight": 7,
    },
    {
        "name": "Synced Review",
        "url": "https://syncedreview.com/feed",
        "type": "rss",
        "categories": ["research", "llm", "general"],
        "weight": 7,
    },
    {
        "name": "WIRED AI",
        "url": "https://www.wired.com/feed/tag/ai/latest/rss",
        "type": "rss",
        "categories": ["general", "llm"],
        "weight": 7,
    },
    {
        "name": "ZDNet AI",
        "url": "https://www.zdnet.com/topic/artificial-intelligence/rss.xml",
        "type": "rss",
        "categories": ["general", "llm"],
        "weight": 6,
    },
    # Community aggregators (signal amplifiers, low individual weight)
    {
        "name": "Reddit r/LocalLLaMA",
        "url": "https://www.reddit.com/r/LocalLLaMA/hot/.rss",
        "type": "rss",
        "categories": ["llm", "code"],
        "weight": 4,
    },
    {
        "name": "Hacker News AI",
        "url": "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT+OR+neural+OR+diffusion+OR+transformer&points=50",
        "type": "rss",
        "categories": ["general", "llm", "code"],
        "weight": 5,
    },
    {
        "name": "Simon Willison",
        "url": "https://simonwillison.net/atom/everything/",
        "type": "rss",
        "categories": ["llm", "general"],
        "weight": 5,
    },
    # Additional primary sources (corporate blogs missing from original seed)
    {
        "name": "NVIDIA AI Blog",
        "url": "https://developer.nvidia.com/blog/feed",
        "type": "rss",
        "categories": ["hardware", "llm", "research"],
        "weight": 8,
    },
    {
        "name": "Google AI Blog",
        "url": "https://blog.google/technology/ai/rss/",
        "type": "rss",
        "categories": ["llm", "research", "general"],
        "weight": 9,
    },
    {
        "name": "LangChain Blog",
        "url": "https://www.langchain.com/blog/rss.xml",
        "type": "rss",
        "categories": ["llm", "code"],
        "weight": 5,
    },
    {
        "name": "Replicate Blog",
        "url": "https://replicate.com/blog/rss",
        "type": "rss",
        "categories": ["image_gen", "llm", "code"],
        "weight": 7,
    },
]


SEED_SOURCES_APPLIED_KEY = "seed_sources_applied"


async def _seed_sources(session: AsyncSession) -> None:
    """Seed sources exactly once per installation.

    Re-seeding on every startup would silently resurrect sources the admin
    deleted via /sources. Existing installations (sources table already
    populated) only get the marker, without re-inserting anything.
    """
    marker = await session.get(Setting, SEED_SOURCES_APPLIED_KEY)
    if marker:
        return

    has_any_source = (
        await session.scalar(select(Source.id).limit(1))
    ) is not None
    if not has_any_source:
        for src in SEED_SOURCES:
            session.add(Source(workspace_id=DEFAULT_WORKSPACE_ID, **src))
            logger.info(f"Seeded source: {src['name']}")

    session.add(Setting(key=SEED_SOURCES_APPLIED_KEY, value="1"))


async def close() -> None:
    await engine.dispose()
    logger.info("Database connection closed")
