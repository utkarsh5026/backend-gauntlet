#!/usr/bin/env bash
# Fast local gate mirroring CI's ruff checks, on exactly the paths CI gates.
#
# Usage:
#   tools/preflight.sh           # ruff format --check
#   tools/preflight.sh --lint    # ruff format --check + ruff check
#
# pyright and pytest are deliberately not here: they take seconds per project and
# belong to `make verify`, not to every commit. This catches what fails CI most.
#
# Escape hatch for git hooks: SKIP_GIT_HOOKS=1 git commit|push ...
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

mode="fmt"
if [[ "${1:-}" == "--lint" ]]; then
  mode="lint"
fi

# The same paths the `python` job in .github/workflows/ci.yml checks.
PATHS=(projects packages bootstrap.py tools/tests)

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  red "✗ uv not found — run: python3 bootstrap.py"
  exit 1
fi

dim "→ preflight: ruff format --check ${PATHS[*]}"
if ! uv run --quiet ruff format --check "${PATHS[@]}"; then
  red "✗ ruff format check failed"
  dim "  Fix with:  uv run ruff format ${PATHS[*]}"
  dim "  Then re-run: make preflight"
  exit 1
fi
green "✓ ruff format"

if [[ "$mode" == "lint" ]]; then
  dim "→ preflight: ruff check ${PATHS[*]}"
  if ! uv run --quiet ruff check "${PATHS[@]}"; then
    red "✗ ruff check failed"
    dim "  Many fixes are automatic:  uv run ruff check --fix ${PATHS[*]}"
    exit 1
  fi
  green "✓ ruff check"
fi

green "✓ preflight OK"
