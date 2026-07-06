# Deployment Guide

Deploy from the repository root. Do not use a Git remote that contains a
token in the URL.

## Required Environment Variables

Create `.env` on the server. Never commit it.

```dotenv
BOT_TOKEN=123456:ABC-DEF...
ADMIN_USER_IDS=[123456789]

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=ainews
POSTGRES_USER=ainews
POSTGRES_PASSWORD=replace-with-strong-password

REDIS_HOST=redis
REDIS_PORT=6379
```

`ADMIN_USER_IDS` are bootstrap admins: startup keeps these users active with
`role=admin`, and `/add_user` cannot demote them.
Production compose requires an explicit `POSTGRES_PASSWORD`; the known
`ainews_dev_password` fallback is dev-only and lives in `docker-compose.dev.yml`.
At least one AI provider must be configured: local smoke-test provider, Codex
CLI provider, OAuth AI Gateway, or OpenRouter. OpenRouter remains the preferred
production fallback when you want direct model API reliability.

Example OpenRouter fallback:

```dotenv
OPENROUTER_API_KEY=sk-or-...
```

For local Telegram smoke tests only, `LOCAL_AI_PROVIDER_ENABLED=true` can be
used instead of real AI provider credentials. Do not enable it in production.

To use the local Codex/ChatGPT login as an AI provider, sign in with the Codex
CLI first and set `CODEX_PROVIDER_ENABLED=true`. The app does not read or store
Codex auth tokens; it invokes `codex exec` and lets Codex manage its own
official login/cache.

### Codex provider in production (Docker)

The production image installs the Codex CLI, but contains no credentials. The
bot container mounts `./codex-home` (next to the compose files on the server)
as `/app/.codex`:

1. On a machine where `codex login` was completed with the ChatGPT account,
   copy the auth file to the server:

   ```bash
   scp ~/.codex/auth.json <server>:/opt/news-parser/codex-home/auth.json
   ```

2. Make the directory writable by the container user (codex rewrites
   auth.json on token refresh). Find the uid with
   `docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --entrypoint id bot`
   and `chown -R <uid>:<gid> codex-home && chmod 700 codex-home && chmod 600 codex-home/auth.json`.

3. Set `CODEX_PROVIDER_ENABLED=true` in the server `.env`.

If `codex exec` fails inside the container with a Landlock/sandbox error, set
`CODEX_SANDBOX=danger-full-access` in `.env` — the container itself is the
isolation boundary; do not use that value outside containers.

## Optional Environment Variables

```dotenv
OPENROUTER_SCORING_MODEL=anthropic/claude-haiku-4.5
OPENROUTER_GENERATION_MODEL=anthropic/claude-sonnet-4.6

# Local Telegram smoke-test only. Generates deterministic drafts without
# external AI calls. Keep false in production.
LOCAL_AI_PROVIDER_ENABLED=false

# Optional Codex CLI provider. Uses official local Codex login/cache and runs
# `codex exec` in a read-only ephemeral session.
CODEX_PROVIDER_ENABLED=false
CODEX_BIN=codex
CODEX_MODEL=
CODEX_TIMEOUT_SECONDS=300
CODEX_SANDBOX=read-only

# Optional OAuth AI Gateway, tried before OpenRouter when configured.
OAUTH_AI_BASE_URL=https://ai-gateway.example/v1
OAUTH_AI_CHAT_COMPLETIONS_PATH=/chat/completions
OAUTH_AI_TOKEN_FILE=.oauth_ai_token.json
OAUTH_AI_SCORING_MODEL=fast-model
OAUTH_AI_GENERATION_MODEL=quality-model
OAUTH_AI_REFRESH_MARGIN_SECONDS=60

# Optional login helper settings for OAuth authorization-code + PKCE.
OAUTH_AI_AUTHORIZATION_URL=https://ai-gateway.example/oauth/authorize
OAUTH_AI_TOKEN_URL=https://ai-gateway.example/oauth/token
OAUTH_AI_CLIENT_ID=replace-with-client-id
OAUTH_AI_REDIRECT_URI=http://127.0.0.1:8765/callback
OAUTH_AI_SCOPES=chat

PARSING_INTERVAL_MINUTES=15
REQUEST_TIMEOUT_SECONDS=30
SCHEDULER_ENABLED=true
TELEGRAM_RSS_SERVICE=https://tg.i-c-a.su
LOG_LEVEL=INFO
LOG_FORMAT=json
```

Use `OAUTH_AI_ACCESS_TOKEN` only for short local tests. For persistent use,
prefer `OAUTH_AI_TOKEN_FILE`; `.oauth_ai_token.json` is ignored by Git.
When the token file contains `refresh_token` and `expires_at`, the runtime
refreshes near-expired access tokens through `OAUTH_AI_TOKEN_URL` before model
calls. Refresh uses `OAUTH_AI_CLIENT_ID` and optional
`OAUTH_AI_CLIENT_SECRET`.

The OAuth AI provider expects a gateway that accepts Bearer tokens and returns a
synchronous generated text response. It supports OpenAI-compatible
`/chat/completions` response shape plus simple `text`, `content`, or
`output_text` JSON fields. ChatGPT browser session tokens, cookies, and scraped
web OAuth credentials must not be used.

To perform a generic OAuth authorization-code + PKCE login for a compatible
gateway:

```bash
scripts/oauth_ai_login.py
```

Open the printed URL, authorize the gateway, and let the local callback write
the token JSON file. If the OAuth provider is incomplete, expired, or returns an
error after retries, the bot falls back to OpenRouter.

## Local Telegram Smoke Test

Use this path to open the bot in Telegram before real AI credentials are ready.
It does not publish anything automatically.

1. In `.env`, set real Telegram credentials and local service hosts:

```dotenv
BOT_TOKEN=123456:ABC-DEF...
ADMIN_USER_IDS=[123456789]
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=ainews
POSTGRES_USER=ainews
POSTGRES_PASSWORD=ainews_dev_password
REDIS_HOST=localhost
REDIS_PORT=6379
LOCAL_AI_PROVIDER_ENABLED=true
LOG_FORMAT=pretty
```

2. Start local PostgreSQL and Redis:

```bash
make up
```

3. Check local runtime readiness without printing secret values:

```bash
make local-check
```

4. Start bot polling:

```bash
make local-bot-start
```

5. In Telegram, open the bot and run the smoke checklist:

- Admin `/start` and `/help`.
- Admin `/health`.
- Admin `/sources`.
- `/queue` to inspect pending items.
- Approve/reject/regenerate/edit when a generated local-test draft appears.

The local AI provider returns deterministic test drafts with
`ai_provider=local_test`, so editorial quality is not representative. It is only
for checking Telegram UX, roles, queue flow, and infrastructure wiring.

To test through Codex instead of the deterministic local provider:

```bash
codex login
```

Then update `.env`:

```dotenv
LOCAL_AI_PROVIDER_ENABLED=false
CODEX_PROVIDER_ENABLED=true
CODEX_SANDBOX=read-only
SCHEDULER_ENABLED=false
```

Run:

```bash
make local-check
make local-bot-start
make local-test-draft
```

Codex provider calls can be slower than direct model APIs because every
generation runs a non-interactive Codex CLI turn. Use it for trusted local
testing or deliberate automation, not as a hidden production default.

To immediately receive a moderation card without waiting for scheduler scoring
thresholds, keep the bot running and execute:

```bash
make local-test-draft
```

The helper creates one smoke cluster, generates a draft through the active AI
provider chain, and sends it to active admins/moderators. With
`LOCAL_AI_PROVIDER_ENABLED=true` the draft is deterministic; with
`CODEX_PROVIDER_ENABLED=true` it is generated through `codex exec`.

Useful local bot commands:

```bash
make local-bot-status
make local-bot-logs
make local-bot-stop
```

## Secrets Handling

- Keep `.env` only on the server or in a secrets manager.
- Use a safe Git remote such as `git@github.com:owner/repo.git` or `https://github.com/owner/repo.git`.
- Do not place GitHub tokens in remotes, docs, commits, or chat.
- Rotate any token that was ever stored in a URL or pasted into an unsafe place.

## Pre-Deploy Check

Run from the repository root:

```bash
scripts/pre_deploy_check.sh
```

The script runs:

- Python syntax compilation for `app`, `scripts`, and `healthcheck.py`.
- Repository safety checks for tracked `.env`/`.env.*` secret files, virtualenv paths at any depth, tokenized HTTP(S) remotes, and obvious API token patterns in tracked plus untracked non-ignored candidate files.
- `pytest`.
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet`, without writing resolved config to disk.
- Docker image build when `SKIP_DOCKER_BUILD` is not set and the Docker daemon is available.

To skip the image build locally:

```bash
POSTGRES_PASSWORD=predeploy_local_placeholder SKIP_DOCKER_BUILD=1 scripts/pre_deploy_check.sh
```

Use a non-secret placeholder only for local config validation. On the server,
run the script with the real `.env` in place. Record the reason for skipping
Docker build in the task/decision log.
If the Docker daemon is unavailable, the script also skips the image build with
a clear message after compose config validation passes.

## Database Migrations

Startup applies unapplied SQL migrations from `scripts/migrations/*.sql`. It no longer runs SQLAlchemy `Base.metadata.create_all()` at runtime or exposes a `create_all()` compatibility alias.

Applied migrations are recorded in the `schema_migrations` table. The Docker image must include `scripts/migrations/`; if the migration directory is missing or contains no SQL files, startup fails instead of silently continuing. If `schema_migrations` has no applied versions but application tables already exist in the current schema, startup fails as an unmanaged schema instead of adopting the baseline automatically. The runner also rejects unapplied migration files older than the newest version already recorded in `schema_migrations`. Schema repair checks are scoped to the current PostgreSQL schema, and old generated defaults on `bot_users.id` are dropped so Telegram IDs remain manually supplied. Migration files are executed inside the runner transaction, so generated migrations should not include `BEGIN` or `COMMIT`.

Create a new migration with:

```bash
python scripts/create_migration.py "describe_change"
```

## Deploy

On the server:

Before changing the running stack, create or verify a fresh PostgreSQL backup
or provider snapshot. Keep it available until the Telegram smoke checklist is
complete.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Follow logs:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
```

## Health Verification

After deployment, verify:

- `docker compose ps` shows healthy `postgres`, `redis`, and running `bot`.
- Bot logs show scheduler startup and parsing cycle completion.
- `/health` in Telegram shows report time, last parsing cycle time, overall status, Scheduler, PostgreSQL, Redis, Telegram API, source, queue counters for `new`/`waiting`/`generating`/`pending_review`, and stuck-generation details.
- Docker `HEALTHCHECK` uses the same health service and returns unhealthy for DB/Redis failures or stale scheduler state.
- Docker `start-period` gives the first parsing cycle up to 300 seconds before health failures count.
- Docker healthcheck timeout is 15 seconds; PostgreSQL, Redis, and Telegram probes are time-bounded and run concurrently inside the shared health service.
- Source errors, Telegram API failures, and stuck generation are degraded states: visible to operators, but they should not restart the bot by themselves.
- Broken sources appear through `Source.last_error`, `/health`, and repeated admin alerts.
- `/queue` shows pending generated articles, including generated articles whose Telegram notification did not reach moderators.
- Viewer cannot clear queue items; moderator can clear one item; admin can clear one item or the current page.
- `/approved` returns approved copy-paste text.

## Telegram Smoke Checklist

Run this after deploy with real bot credentials and real role accounts. Record the
result in the deploy log before calling the release ready.

- [ ] Admin `/start` and `/help` work, and `/help` shows admin-only commands.
- [ ] Moderator `/start` and `/help` work, without source management or `/health`.
- [ ] Viewer `/start`, `/help`, and `/approved` work, without destructive queue controls.
- [ ] Non-admin `/health` is denied; admin `/health` shows report time, last cycle time, component statuses, source counters, and queue counters.
- [ ] `/sources` as admin shows configured sources; toggling one source changes its state and can be reverted.
- [ ] A parsing cycle completes after startup, updates `/health`, and records source errors instead of hiding parser failures.
- [ ] A real or controlled source item reaches scoring, generation, and moderator notification.
- [ ] Moderator approve moves the latest draft to approved, and viewer can retrieve copy-paste text through `/approved`.
- [ ] Moderator reject marks the draft/cluster rejected and does not expose it through `/approved`.
- [ ] Regenerate/edit creates a newer draft; stale buttons from the older draft are denied.
- [ ] `/queue` remains a fallback for pending review drafts, including zero-delivery notifications.

## Rollback

Use the previous known-good Git commit or image only when the database schema is
still compatible with that version:

```bash
git checkout <previous-good-commit>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Forward SQL migrations may make an old image incompatible with the current
schema. If rollback crosses a migration boundary, restore the pre-deploy
PostgreSQL backup/snapshot or roll forward with a corrective migration/fix
instead of assuming code/image rollback is enough. Then verify logs and
`/health` again.

## Current Production Caveats

- Docker healthcheck verifies scheduler recency, PostgreSQL, Redis, and Telegram Bot API reachability.
- Telegram smoke checks require real bot credentials and should be done by the operator after deploy.
## Client Workspaces (Multi-Tenant)

1. Add the bot to the client's Telegram group; a group admin runs `/setup`,
   then `/set_topic` and optionally `/set_keywords`.
2. The workspace owner DMs the bot `/connect_openrouter` and pastes their
   OpenRouter key (BYOK). Without a key the workspace does not generate.
3. Sources are managed in the group via `/add_source` and `/sources`
   (group admins); moderation cards arrive in the group and are actionable
   by its members.

The owner's DM workspace keeps the original behavior and the global provider
chain (including the Codex provider).
