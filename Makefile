# backend-gauntlet — root task runner.
# Per-project tasks live in projects/NN-*/Makefile. This root one is for things
# that span every project — chiefly the cross-project progress dashboard.

.DEFAULT_GOAL := status

.PHONY: status
status: ## Progress dashboard across all projects (pass NN to drill in: make status NN=02)
	@python3 status.py $(NN)

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
