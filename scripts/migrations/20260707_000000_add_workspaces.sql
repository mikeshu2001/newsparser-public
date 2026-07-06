-- Multi-tenant core: workspaces own sources and news clusters. Existing data
-- is backfilled into the default workspace (id 1 — the instance owner's,
-- bound to DM mode; group workspaces bind to a Telegram chat_id).
CREATE TABLE workspaces (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE,
    name VARCHAR(255) NOT NULL,
    topic TEXT,
    keywords TEXT,
    news_prompt TEXT,
    tone_of_voice TEXT,
    openrouter_api_key TEXT,
    generation_model VARCHAR(100),
    scoring_model VARCHAR(100),
    score_threshold INTEGER,
    hot_threshold INTEGER,
    cluster_wait_minutes INTEGER,
    active BOOLEAN NOT NULL DEFAULT true,
    owner_user_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO workspaces (name, topic)
VALUES ('Основной', 'Новости нейросетей и искусственного интеллекта');

ALTER TABLE sources
    ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1
        REFERENCES workspaces(id) ON DELETE CASCADE;
ALTER TABLE sources ALTER COLUMN workspace_id DROP DEFAULT;
ALTER TABLE sources DROP CONSTRAINT sources_url_key;
ALTER TABLE sources ADD CONSTRAINT uq_sources_workspace_url UNIQUE (workspace_id, url);
CREATE INDEX idx_sources_workspace ON sources(workspace_id);

ALTER TABLE news_clusters
    ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1
        REFERENCES workspaces(id) ON DELETE CASCADE;
ALTER TABLE news_clusters ALTER COLUMN workspace_id DROP DEFAULT;
CREATE INDEX idx_clusters_workspace ON news_clusters(workspace_id);
