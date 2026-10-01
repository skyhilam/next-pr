#!/usr/bin/env bash
# Create an isolated worktree + branch + draft PR for skyhilam/dkdm-monorepo.
# Deterministic git/gh steps so the model does not re-invent them each run.
#
# Usage:
#   open-pr.sh --title "short title" --body-file /path/to/body.md [--branch NAME]
#
# Env (optional):
#   NEXT_PR_REPO   default skyhilam/dkdm-monorepo
#   NEXT_PR_BASE   default main
#
# Prints JSON on stdout:
#   { "branch", "worktree", "pr_number", "pr_url", "head_sha" }
set -euo pipefail

REPO="${NEXT_PR_REPO:-skyhilam/dkdm-monorepo}"
BASE="${NEXT_PR_BASE:-main}"
TITLE=""
BODY_FILE=""
BRANCH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title) TITLE="${2:-}"; shift 2 ;;
    --body-file) BODY_FILE="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$TITLE" || -z "$BODY_FILE" ]]; then
  echo "need --title and --body-file" >&2
  exit 2
fi
if [[ ! -f "$BODY_FILE" ]]; then
  echo "body file not found: $BODY_FILE" >&2
  exit 2
fi

# Resolve shared checkout from current directory's git (first worktree path).
ORIGIN="$(git remote get-url origin 2>/dev/null || true)"
case "$ORIGIN" in
  *skyhilam/dkdm-monorepo*) ;;
  *)
    echo "origin is not skyhilam/dkdm-monorepo (got: ${ORIGIN:-none}). Refuse to open." >&2
    exit 1
    ;;
esac

SHARED="$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
if [[ -z "$SHARED" || ! -d "$SHARED" ]]; then
  echo "could not resolve shared checkout from git worktree list" >&2
  exit 1
fi

SLUG="$(printf '%s' "$TITLE" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' | cut -c1-40)"
[[ -n "$SLUG" ]] || SLUG="change"
if [[ -z "$BRANCH" ]]; then
  BRANCH="next-pr/${SLUG}-$(date +%Y%m%d%H%M%S)"
fi

PARENT="$(dirname "$SHARED")"
WORKTREE="${PARENT}/next-pr-worktrees/${BRANCH//\//-}"
mkdir -p "$(dirname "$WORKTREE")"

git -C "$SHARED" fetch origin "$BASE"
if [[ -e "$WORKTREE" ]]; then
  echo "worktree path already exists: $WORKTREE" >&2
  exit 1
fi

git -C "$SHARED" worktree add -b "$BRANCH" "$WORKTREE" "origin/${BASE}"
git -C "$WORKTREE" commit --allow-empty -m "draft: ${TITLE}"
git -C "$WORKTREE" push -u origin "$BRANCH"

PR_URL="$(gh pr create --repo "$REPO" --draft --base "$BASE" --head "$BRANCH" \
  --title "$TITLE" --body-file "$BODY_FILE")"
PR_NUMBER="$(gh pr view "$PR_URL" --repo "$REPO" --json number -q .number)"
HEAD_SHA="$(git -C "$WORKTREE" rev-parse HEAD)"

python3 - <<PY
import json
print(json.dumps({
  "branch": "$BRANCH",
  "worktree": "$WORKTREE",
  "pr_number": int("$PR_NUMBER"),
  "pr_url": "$PR_URL",
  "head_sha": "$HEAD_SHA",
  "shared_checkout": "$SHARED",
}, ensure_ascii=False))
PY
