-- Migration: schema_updates
-- Current idempotent schema updates previously applied by database._ensure_schema_updates().

ALTER TABLE news_clusters ADD COLUMN IF NOT EXISTS topic_original VARCHAR(500);

-- Convert all TIMESTAMP columns to TIMESTAMPTZ (timezone-aware).
-- PostgreSQL treats existing values as UTC when converting.
DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'sources'
	          AND column_name = 'created_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE sources ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'sources'
	          AND column_name = 'last_checked_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE sources ALTER COLUMN last_checked_at TYPE TIMESTAMPTZ USING last_checked_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'news_clusters'
	          AND column_name = 'first_seen_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE news_clusters ALTER COLUMN first_seen_at TYPE TIMESTAMPTZ USING first_seen_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'news_clusters'
	          AND column_name = 'created_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE news_clusters ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'news_clusters'
	          AND column_name = 'updated_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE news_clusters ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'raw_articles'
	          AND column_name = 'published_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE raw_articles ALTER COLUMN published_at TYPE TIMESTAMPTZ USING published_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'raw_articles'
	          AND column_name = 'fetched_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE raw_articles ALTER COLUMN fetched_at TYPE TIMESTAMPTZ USING fetched_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'generated_articles'
	          AND column_name = 'created_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE generated_articles ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'bot_users'
	          AND column_name = 'created_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE bot_users ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
	    IF EXISTS (
	        SELECT 1 FROM information_schema.columns
	        WHERE table_schema = current_schema()
	          AND table_name = 'settings'
	          AND column_name = 'updated_at'
	          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE settings ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC';
    END IF;
	END $$;

ALTER TABLE bot_users ALTER COLUMN id DROP DEFAULT;
