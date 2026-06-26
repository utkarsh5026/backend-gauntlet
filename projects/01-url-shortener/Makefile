# url-shortener — local dev shortcuts
#
# Run from this directory:  make help
# Requires: docker, cargo, and (for migrate) sqlx-cli

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
WORKSPACE   := $(abspath $(PROJECT_DIR)/../..)
CRATE       := url-shortener
COMPOSE     := docker compose
CARGO       = cd $(WORKSPACE) && cargo

# Load .env for migrate / run targets (optional until `make setup`).
ifneq (,$(wildcard $(PROJECT_DIR)/.env))
include $(PROJECT_DIR)/.env
export
endif

.DEFAULT_GOAL := help

.PHONY: help setup install-tools deps up down ps logs wait-db migrate reset-db \
        check clippy fmt fmt-check test run dev verify clean smoke

help: ## Show this help
	@printf '\nurl-shortener — common commands\n\n'
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nTypical first run:\n  make setup && make deps && make migrate && make run\n'
	@printf 'Run all checks:        make verify\n\n'

setup: ## Copy .env.example → .env (skip if .env exists)
	@if [ -f .env ]; then \
		echo ".env already exists — not overwriting"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi

install-tools: ## Install sqlx-cli for migrations (Postgres only)
	cargo install sqlx-cli --no-default-features --features rustls,postgres

deps: up ## Alias for `up`

up: ## Start Postgres + Redis (docker compose up -d)
	$(COMPOSE) up -d
	@$(MAKE) wait-db

down: ## Stop docker services
	$(COMPOSE) down

ps: ## Show docker service status
	$(COMPOSE) ps

logs: ## Follow docker logs
	$(COMPOSE) logs -f

wait-db: ## Block until Postgres accepts connections
	@echo "Waiting for Postgres…"
	@for i in $$(seq 1 30); do \
		if $(COMPOSE) exec -T postgres pg_isready -U shortener >/dev/null 2>&1; then \
			echo "Postgres is ready."; \
			exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "Postgres did not become ready in time." >&2; \
	exit 1

migrate: setup ## Apply SQL migrations (needs sqlx-cli + DATABASE_URL)
	@command -v sqlx >/dev/null 2>&1 || { \
		echo "sqlx-cli not found. Run: make install-tools" >&2; \
		exit 1; \
	}
	cd $(PROJECT_DIR) && sqlx migrate run

reset-db: ## Drop volumes and recreate DB (destructive)
	$(COMPOSE) down -v
	$(COMPOSE) up -d
	@$(MAKE) wait-db
	@$(MAKE) migrate

check: ## cargo check this crate
	$(CARGO) check -p $(CRATE)

clippy: ## cargo clippy with warnings denied
	$(CARGO) clippy -p $(CRATE) -- -D warnings

fmt: ## Format workspace Rust code
	$(CARGO) fmt --all

fmt-check: ## Fail if code is not formatted
	$(CARGO) fmt --all -- --check

test: ## Run crate tests
	$(CARGO) test -p $(CRATE)

run: setup ## Run the server (loads .env)
	$(CARGO) run -p $(CRATE)

dev: deps migrate run ## Start deps, migrate, then run server

verify: fmt-check clippy check test ## Run all static checks + tests
	@echo "verify: OK"

clean: ## cargo clean for this crate
	$(CARGO) clean -p $(CRATE)

smoke: ## Hit /healthz (server must already be running)
	@curl -sf "http://localhost:$${PORT:-8080}/healthz" && echo
