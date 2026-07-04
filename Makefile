# backend-gauntlet — root task runner.
# Per-project tasks live in projects/NN-*/Makefile. This root one is for things
# that span every project — chiefly the cross-project progress dashboard.

.DEFAULT_GOAL := status

.PHONY: status
status: ## Progress dashboard across all projects (pass NN to drill in: make status NN=02)
	@python3 status.py $(NN)

.PHONY: trophies
trophies: ## 🏆 Trophy case — achievements derived from code, SPECs, and git history
	@python3 status.py trophies

.PHONY: infra
infra: ## Web control panel for each project's Docker deps (up/down + port collisions)
	@python3 infra.py $(if $(PORT),--port $(PORT),)

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
