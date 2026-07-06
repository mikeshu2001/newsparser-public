from __future__ import annotations

import re
import unicodedata

# Generic terms that don't identify a specific brand/topic.
# Used by dedup.py for adaptive entity overlap threshold.
GENERIC_TERMS: set[str] = {
    'ai', 'ml', 'model', 'llm', 'gpu', 'api', 'open-source', 'open source',
    'launch', 'release', 'new', 'update', 'version', 'blog', 'via',
    'research', 'paper', 'report', 'tool', 'app', 'product',
}


def truncate(text: str, max_length: int = 4096, suffix: str = "...") -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


_MD_ESCAPE_CHARS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md(text: str) -> str:
    return _MD_ESCAPE_CHARS.sub(r"\\\1", text)


# --- Phase 3: dedup & scoring utilities ---

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")

# Stop-words that don't carry meaning for fuzzy matching
_STOP_WORDS: set[str] = {
    # EN
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to",
    "for", "of", "and", "or", "but", "with", "by", "from", "its", "it",
    "this", "that", "has", "have", "had", "be", "been", "will", "can",
    "not", "no", "as", "we", "our", "new",
    # RU
    "и", "в", "на", "с", "по", "для", "из", "от", "до", "что", "как",
    "это", "не", "но", "за", "его", "она", "они", "все", "уже", "при",
    "свой", "свою", "нет", "да", "также", "или", "ещё", "еще", "он",
    "был", "была", "были", "будет", "новый", "новая", "новое", "которая",
    "который", "которое",
}


def normalize_title(title: str) -> str:
    """Normalize title for fuzzy comparison.

    Lowercase, strip accents, remove punctuation and stop-words.
    """
    text = title.lower()
    # Strip accents (NFD → remove combining marks → NFC)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = unicodedata.normalize("NFC", text)
    # Remove punctuation
    text = _PUNCT_RE.sub(" ", text)
    # Remove stop-words
    words = text.split()
    words = [w for w in words if w not in _STOP_WORDS]
    return _MULTI_SPACE_RE.sub(" ", " ".join(words)).strip()


# Brand / entity extraction for cross-language dedup
BRAND_PATTERN = re.compile(
    r"\b("
    r"OpenAI|ChatGPT|GPT-\d\S*|Claude|Anthropic|Gemini|Google|DeepMind|"
    r"Meta|Llama|Mistral|Midjourney|DALL-E|Stable Diffusion|Flux|"
    r"Sora|Runway|Pika|Copilot|Cursor|NVIDIA|H\d{3}|"
    r"DeepSeek|Qwen|Grok|xAI|Perplexity|Cohere|LangChain|LangGraph|LlamaIndex|"
    r"Groq|Together\s?AI|Fireworks\s?AI|Replicate|Windsurf|Bolt|Lovable|"
    r"Character\.AI|ElevenLabs|Suno|Udio|Luma|Kling|Hailuo|MiniMax|Kimi|Moonshot|"
    r"AMD|Intel|B\d{3}|A\d{3}|RTX\s?\d{4}"
    r")\b",
    re.IGNORECASE,
)

# CamelCase words like "NovaMind", "DeepCoder"
_CAMEL_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
# All-caps abbreviations (2-10 chars), e.g. FLUX, KLING — after filtering common ones
_UPPER_RE = re.compile(r"\b[A-Z]{2,10}\b")
# Versioned tokens like "GPT-5", "V2.5", "M3.1"
_VERSIONED_RE = re.compile(r"\b[A-Za-z]+[-.]?\d+(?:\.\d+)?\b")

# Common abbreviations that are NOT brand names and should not be treated as entities
COMMON_ABBR: frozenset[str] = frozenset({
    "AI", "ML", "NLP", "API", "GPU", "CPU", "TPU", "LLM", "RSS", "URL",
    "HTTP", "SDK", "CLI", "RAM", "SSD", "PDF", "NEW", "THE", "FOR", "AND",
    "NOT", "BUT", "HOW", "WHY", "CEO", "CTO", "CFO", "COO", "VP", "PR",
    "US", "UK", "EU", "AGI", "ASI", "SFT", "DPO", "PPO", "RLHF", "RAG",
    "TTS", "STT", "VLM", "MOE", "MCP", "AWS", "GCP", "VPS", "CEO", "CTO",
    "IPO",
})

# Versioned-looking tokens that are calendar/business terms, not products
COMMON_VERSIONED: frozenset[str] = frozenset({
    "q1", "q2", "q3", "q4", "h1", "h2", "fy24", "fy25", "fy26",
})


def extract_entities(title: str) -> set[str]:
    """Extract brand/product entities from title using 4 heuristic steps.

    Step 1: Known brands via BRAND_PATTERN (high precision).
    Step 2: CamelCase words (NovaMind, DeepCoder) — catches unknown brands.
    Step 3: ALL-CAPS words excluding common abbreviations (FLUX, KLING).
    Step 4: Versioned tokens (GPT-5, V2.5, M3.1).
    """
    entities: set[str] = set()

    # Step 1: known brands
    for m in BRAND_PATTERN.finditer(title):
        entities.add(m.group(0).lower())

    # Step 2: CamelCase
    for w in _CAMEL_RE.findall(title):
        entities.add(w.lower())

    # Step 3: UPPER CASE, excluding common abbreviations
    for w in _UPPER_RE.findall(title):
        if w not in COMMON_ABBR:
            entities.add(w.lower())

    # Step 4: versioned tokens
    for w in _VERSIONED_RE.findall(title):
        if w.lower() in COMMON_VERSIONED:
            continue
        entities.add(w.lower())

    return entities


# Trigger phrases for scoring bonus
TRIGGER_PHRASES_EN = [
    "launching", "introducing", "announcing", "releasing",
    "new model", "open source", "available now", "just released",
    "we're excited", "breaking",
]
TRIGGER_PHRASES_RU = [
    "запускаем", "представляем", "анонсируем", "выпустили",
    "новая модель", "открытый код", "доступно", "вышла",
]

_ALL_TRIGGERS = TRIGGER_PHRASES_EN + TRIGGER_PHRASES_RU


def has_trigger_phrases(text: str) -> bool:
    """Check if text contains any trigger phrases (launch announcements, etc.)."""
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in _ALL_TRIGGERS)
