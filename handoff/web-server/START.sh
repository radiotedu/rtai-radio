#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PROMPT="$PROJECT_ROOT/handoff/web-server/prompt.md"
test -f "$PROMPT" || { echo "Missing website Codex prompt: $PROMPT" >&2; exit 1; }
cd "$PROJECT_ROOT"
echo "RadioTEDU website-server handoff (read-only starter)."
if test -d .git; then echo "Transferred revision: $(git rev-parse HEAD)"; fi
echo "Open the following prompt in Codex on this target computer:"
echo "$PROMPT"
echo
cat "$PROMPT"
