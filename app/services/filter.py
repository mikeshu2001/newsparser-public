from __future__ import annotations

import re
from typing import Optional

from app.database.models import Workspace
from app.parsers.base import ParsedArticle

# Level 1 keyword filter — fast, no AI.
# If ANY keyword is found in title + first 500 chars of content, article passes.
# Sources with weight >= 9 (official blogs like OpenAI, Anthropic, DeepMind)
# are always relevant — skip filtering for them.

AI_KEYWORDS: set[str] = {
    # EN — general terms
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "large language model", "llm",
    "transformer", "diffusion", "generative ai", "gen ai",
    "chatbot", "ai model", "ai agent", "fine-tuning", "finetuning", "rag",
    "computer vision", "nlp", "reinforcement learning",
    "text-to-image", "text-to-video", "text-to-speech",
    "open source ai", "foundation model", "multimodal",
    # EN — technical terms
    "open-source", "quantization", "gguf", "ggml", "lora", "qlora",
    "retrieval augmented", "vector database", "embedding", "context window",
    "mixture of experts", "benchmark", "leaderboard", "mmlu", "humaneval",
    "ai safety", "alignment", "constitutional ai",
    "h200", "b100", "b200", "inference", "training run",
    "image generation", "video generation", "code generation",
    "ai coding", "agentic", "tool use", "function calling",
    "open weight",
    # EN — brands (established)
    "openai", "chatgpt", "gpt-4", "gpt-5", "claude", "anthropic",
    "gemini", "google ai", "deepmind", "llama", "mistral",
    "midjourney", "dall-e", "stable diffusion", "flux",
    "sora", "runway", "pika", "copilot", "cursor", "devin",
    "nvidia", "h100", "a100",
    # EN — new brands
    "deepseek", "qwen", "grok", "xai", "perplexity", "cohere", "langchain",
    "groq", "together ai", "fireworks ai", "replicate", "windsurf",
    "character ai", "elevenlabs", "suno", "udio", "luma ai", "kling", "hailuo",
    "minimax", "kimi", "moonshot ai",
    # RU — general terms
    "искусственный интеллект", "нейросеть", "нейронная сеть",
    "машинное обучение", "глубокое обучение", "языковая модель",
    "генеративный ии", "чат-бот", "диффузия",
    # RU — additional terms
    "генерация изображений", "генерация видео", "генерация музыки",
    "генерация кода", "дообучение", "квантизация", "мультимодальн",
    "открытая модель", "бенчмарк",
}

# Deliberate stems that must match word prefixes (no trailing boundary)
_PREFIX_KEYWORDS: set[str] = {"мультимодальн"}


def _keyword_regex(keyword: str) -> str:
    pattern = r"\b" + re.escape(keyword)
    if keyword not in _PREFIX_KEYWORDS:
        pattern += r"\b"
    return pattern


# Pre-compile a regex pattern for faster matching.  Word boundaries prevent
# substring false positives ('raised' in 'praised', 'udio' in 'studio').
_KEYWORDS_PATTERN = re.compile(
    "|".join(_keyword_regex(kw) for kw in sorted(AI_KEYWORDS, key=len, reverse=True)),
    re.IGNORECASE,
)


# Compiled per-workspace keyword patterns, invalidated when keywords change.
_workspace_patterns: dict[int, tuple[str, re.Pattern[str]]] = {}


def _workspace_pattern(workspace: Optional[Workspace]) -> Optional[re.Pattern[str]]:
    if workspace is None or not (workspace.keywords or "").strip():
        return None

    raw = workspace.keywords.strip()
    cached = _workspace_patterns.get(workspace.id)
    if cached is not None and cached[0] == raw:
        return cached[1]

    keywords = sorted(
        {
            keyword.strip().lower()
            for line in raw.splitlines()
            for keyword in line.split(",")
            if keyword.strip()
        },
        key=len,
        reverse=True,
    )
    pattern = re.compile(
        "|".join(_keyword_regex(keyword) for keyword in keywords),
        re.IGNORECASE,
    )
    _workspace_patterns[workspace.id] = (raw, pattern)
    return pattern


def is_relevant(
    article: ParsedArticle,
    source_weight: int = 5,
    workspace: Optional[Workspace] = None,
) -> bool:
    """Check if article passes Level 1 keyword filter.

    Sources with weight >= 9 always pass (dedicated topical sources).
    For general sources: check title + first 500 chars of content against the
    workspace's keyword list, falling back to the built-in AI set.
    """
    if source_weight >= 9:
        return True

    text = article.title
    if article.content:
        text += " " + article.content[:500]

    pattern = _workspace_pattern(workspace) or _KEYWORDS_PATTERN
    return bool(pattern.search(text))
