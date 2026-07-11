# backend-gauntlet — root task runner.
# Per-project tasks live in projects/NN-*/Makefile. This root one is for things
# that span every project — chiefly the cross-project progress dashboard.

.DEFAULT_GOAL := status

.PHONY: status
status: ## Progress dashboard across all projects (pass NN to drill in: make status NN=02)
	@python3 tools/status.py $(NN)

.PHONY: trophies
trophies: ## 🏆 Trophy case — achievements derived from code, SPECs, and git history
	@python3 tools/status.py trophies

.PHONY: infra
infra: ## Web control panel for each project's Docker deps (up/down + port collisions)
	@python3 tools/infra.py $(if $(PORT),--port $(PORT),)

.PHONY: dev
dev: ## One-window dev stack: deps + server + frontend (make dev NN=01; multi: NN="01 03")
	@python3 tools/dev.py $(NN)

.PHONY: portainer
portainer: ## Start Portainer — web UI for all containers (https://localhost:9443)
	@docker compose -f tools/portainer/docker-compose.yml -p portainer up -d
	@echo "Portainer → https://localhost:9443  (accept the self-signed cert; set an admin password on first visit)"

.PHONY: portainer-down
portainer-down: ## Stop and remove Portainer
	@docker compose -f tools/portainer/docker-compose.yml -p portainer down

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
