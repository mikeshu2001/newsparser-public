-- Dedicated lifecycle timestamp: status timers (stuck-generation recovery,
-- waiting delay) must not depend on updated_at, which is bumped by dedup and
-- scoring on every matched article.
ALTER TABLE news_clusters
    ADD COLUMN status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now();
