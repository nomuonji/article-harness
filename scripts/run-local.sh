#!/usr/bin/env bash
# Run from a WSL/Linux shell. Paths passed to --repo and --state are Linux paths.
set -euo pipefail
HARNESS_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$HARNESS_ROOT"
DRY_RUN=0
for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then DRY_RUN=1; fi
done
if [ "$DRY_RUN" -eq 0 ] && ! command -v codex >/dev/null 2>&1; then
  echo 'Codex CLI is not on PATH in this WSL shell. Run codex login or fix your shell PATH.' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.11+ is required.' >&2
  exit 1
fi
exec python3 -m article_harness.local \
  --template "$HARNESS_ROOT/config/qualification-pilot.json" "$@"
