.PHONY: up down logs run psql redis-cli local-check local-bot-start local-bot-stop local-bot-status local-bot-logs local-test-draft build prod-up prod-down prod-logs

up:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

down:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml down

logs:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f

run:
	.venv/bin/python -m app.main

psql:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres psql -U $${POSTGRES_USER:-ainews} -d $${POSTGRES_DB:-ainews}

redis-cli:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml exec redis redis-cli

local-check:
	.venv/bin/python scripts/local_runtime_check.py --prepare-db --check-ai

local-check-telegram:
	.venv/bin/python scripts/local_runtime_check.py --prepare-db --telegram --check-ai

local-bot-start:
	@command -v screen >/dev/null || { echo "screen is required for detached local bot"; exit 1; }
	@screen -S news-parser-bot -X quit >/dev/null 2>&1 || true
	@rm -f .local_bot.log .local_bot.pid
	@screen -dmS news-parser-bot zsh -lc 'cd "$(CURDIR)" && echo $$$$ > .local_bot.pid && env PYTHONUNBUFFERED=1 .venv/bin/python -m app.main >> .local_bot.log 2>&1'
	@sleep 3
	@screen -ls | grep -q news-parser-bot && echo "Local bot started in screen session news-parser-bot" || { echo "Local bot failed to start"; tail -n 80 .local_bot.log; exit 1; }

local-bot-stop:
	@screen -S news-parser-bot -X quit >/dev/null 2>&1 || true
	@echo "Local bot stopped"

local-bot-status:
	@screen -ls | grep -q news-parser-bot && echo "screen session: running" || echo "screen session: not running"
	@if test -f .local_bot.pid && kill -0 "$$(cat .local_bot.pid)" 2>/dev/null; then echo "process: running pid $$(cat .local_bot.pid)"; else echo "process: not detected"; fi

local-bot-logs:
	tail -f .local_bot.log

local-test-draft:
	.venv/bin/python scripts/create_local_test_draft.py

build:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml build

prod-up:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

prod-down:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml down

prod-logs:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
