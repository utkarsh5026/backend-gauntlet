#!/usr/bin/env bash
# Fast local gate mirroring CI's fmt (+ optional hakari) checks.
#
# Usage:
#   tools/preflight.sh           # fmt --check
#   tools/preflight.sh --lint    # fmt --check + hakari (if cargo-hakari is installed)
#
# Escape hatch for git hooks: SKIP_GIT_HOOKS=1 git commit|push ...
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

mode="fmt"
if [[ "${1:-}" == "--lint" ]]; then
  mode="lint"
fi

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

fail_fmt() {
  red "✗ rustfmt check failed"
  dim "  Fix with:  cargo fmt --all"
  dim "  Then re-run: make preflight"
  exit 1
}

dim "→ preflight: cargo fmt --all -- --check"
cargo fmt --all -- --check || fail_fmt
green "✓ rustfmt"

if [[ "$mode" == "lint" ]]; then
  if cargo hakari --version >/dev/null 2>&1; then
    dim "→ preflight: cargo hakari generate --diff"
    cargo hakari generate --diff --quiet
    dim "→ preflight: cargo hakari manage-deps --dry-run"
    cargo hakari manage-deps --dry-run --quiet
    green "✓ hakari"
  else
    dim "↷ skipping hakari (cargo-hakari not installed)"
  fi
fi

green "✓ preflight OK"
