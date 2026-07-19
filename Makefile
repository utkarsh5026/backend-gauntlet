# backend-gauntlet — root task runner.
# Per-project tasks live in projects/NN-*/Makefile. This root one is for things
# that span every project — chiefly the cross-project progress dashboard.

.DEFAULT_GOAL := status

.PHONY: status
status: ## Progress dashboard across all projects (pass NN to drill in: make status NN=02)
	@python3 tools/status.py $(NN)

.PHONY: status-readme
status-readme: ## Refresh the README.md progress block from `make status`
	@python3 tools/update_readme_status.py

.PHONY: trophies
trophies: ## 🏆 Trophy case — achievements derived from code, SPECs, and git history
	@python3 tools/status.py trophies

.PHONY: infra
infra: ## Web control panel for each project's Docker deps (up/down + port collisions)
	@python3 tools/infra.py $(if $(PORT),--port $(PORT),)

.PHONY: dev
dev: ## One-window dev stack: deps + server + frontend (make dev NN=01; multi: NN="01 03")
	@python3 tools/dev.py $(NN)

.PHONY: md
md: ## View project markdown in glow (make md NN=01 [FILE=SPEC.md])
	@python3 tools/md.py $(NN) $(if $(FILE),--file $(FILE),)

# ── per-project status cards (auto-generated — zero upkeep) ──────────────────
# Every project under projects/NN-* gets two targets that open its detailed
# status card:  `make url-shortener`  and the short  `make 01`.
# New projects light up automatically; nothing here to hand-maintain.
PROJECT_SLUGS := $(notdir $(sort $(wildcard projects/[0-9][0-9]-*)))
PROJECT_NUMS  := $(foreach s,$(PROJECT_SLUGS),$(firstword $(subst -, ,$(s))))
PROJECT_NAMES := $(foreach s,$(PROJECT_SLUGS),$(patsubst $(firstword $(subst -, ,$(s)))-%,%,$(s)))

.PHONY: $(PROJECT_NAMES) $(PROJECT_NUMS) projects

# One recipe, two target names (the full name + its NN) → status.py NN.
# Pass FULL=1 to expand every acceptance box, not just the open ones.
define PROJECT_RULE
$(patsubst $(firstword $(subst -, ,$(1)))-%,%,$(1)) $(firstword $(subst -, ,$(1))):
	@python3 tools/status.py $(firstword $(subst -, ,$(1))) $(if $(FULL),full,)
endef
$(foreach s,$(PROJECT_SLUGS),$(eval $(call PROJECT_RULE,$(s))))

projects: ## List the per-project status shortcuts (make <name> or make NN)
	@echo "per-project status cards — run 'make <name>' or the short 'make NN':"
	@for s in $(PROJECT_SLUGS); do \
		n=$${s%%-*}; nm=$${s#*-}; \
		printf '  make %-22s (make %s)\n' "$$nm" "$$n"; \
	done
	@echo "  (add FULL=1 to expand every box, e.g. make object-store FULL=1)"

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
