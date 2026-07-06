-- Migration: initial_schema
-- Baseline schema for migration-only startup.

CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    url VARCHAR(500) NOT NULL UNIQUE,
    type VARCHAR(20) NOT NULL CHECK (type IN ('rss', 'twitter', 'telegram', 'web', 'sitemap')),
    categories VARCHAR(50)[] NOT NULL DEFAULT '{}',
    weight INTEGER NOT NULL DEFAULT 5,
    scraper_config JSONB,
    active BOOLEAN NOT NULL DEFAULT true,
    added_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    last_error TEXT,
    etag VARCHAR(255),
    last_modified VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS news_clusters (
    id SERIAL PRIMARY KEY,
    topic VARCHAR(500),
    topic_original VARCHAR(500),
    category VARCHAR(50) NOT NULL DEFAULT 'general',
    news_type VARCHAR(50),
    score INTEGER NOT NULL DEFAULT 0,
    is_hot BOOLEAN NOT NULL DEFAULT false,
    status VARCHAR(20) NOT NULL DEFAULT 'new' CHECK (
        status IN ('new', 'waiting', 'generating', 'pending_review', 'approved', 'rejected')
    ),
    sources_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_articles (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id VARCHAR(500),
    title TEXT NOT NULL,
    content TEXT,
    url VARCHAR(500),
    language VARCHAR(5) NOT NULL DEFAULT 'en',
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cluster_id INTEGER REFERENCES news_clusters(id) ON DELETE SET NULL,
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS generated_articles (
    id SERIAL PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES news_clusters(id) ON DELETE CASCADE,
    headline TEXT NOT NULL,
    body TEXT NOT NULL,
    summary VARCHAR(300),
    ai_provider VARCHAR(50),
    version INTEGER NOT NULL DEFAULT 1,
    edit_comment TEXT,
    moderated_by BIGINT,
    telegram_message_id BIGINT,
    telegram_chat_id BIGINT,
    status VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_users (
    id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    role VARCHAR(20) NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'moderator', 'viewer')),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clusters_status ON news_clusters (status);
CREATE INDEX IF NOT EXISTS idx_clusters_created ON news_clusters (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clusters_score ON news_clusters (score DESC);
CREATE INDEX IF NOT EXISTS idx_raw_articles_fetched ON raw_articles (fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_articles_cluster ON raw_articles (cluster_id);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON generated_articles (cluster_id);
CREATE INDEX IF NOT EXISTS idx_articles_status ON generated_articles (status);
