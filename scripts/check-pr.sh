#!/usr/bin/env bash
# Observe PR / head / CI facts for the current (or given) repo. Read-only.
#
# Usage:
#   check-pr.sh <pr-number-or-url> [--repo owner/name]
#
# Env: NEXT_PR_REPO overrides detection from origin when --repo omitted.
set -euo pipefail

TARGET=""
REPO="${NEXT_PR_REPO:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      if [[ -z "$TARGET" ]]; then TARGET="$1"; shift
      else echo "unexpected arg: $1" >&2; exit 2
      fi
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "usage: check-pr.sh <pr-number-or-url> [--repo owner/name]" >&2
  exit 2
fi

if [[ -z "$REPO" ]]; then
  ORIGIN="$(git remote get-url origin 2>/dev/null || true)"
  if [[ -n "$ORIGIN" ]]; then
    ORIGIN="${ORIGIN%.git}"
    if [[ "$ORIGIN" =~ [:/]([^/]+)/([^/]+)$ ]]; then
      REPO="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    fi
  fi
fi
if [[ -z "$REPO" ]]; then
  echo "could not determine repo; pass --repo owner/name" >&2
  exit 1
fi

PR_JSON="$(gh pr view "$TARGET" --repo "$REPO" --json \
  number,url,title,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName,statusCheckRollup,reviews,comments)"

python3 - <<'PY' "$PR_JSON" "$REPO"
import json, sys

pr = json.loads(sys.argv[1])
repo = sys.argv[2]
head = pr.get("headRefOid") or ""

latest = {}
for c in pr.get("statusCheckRollup") or []:
    name = c.get("name") or c.get("context")
    if not name:
        continue
    stamp = c.get("completedAt") or c.get("startedAt") or ""
    cur = latest.get(name)
    cur_stamp = (cur.get("completedAt") or cur.get("startedAt") or "") if cur else ""
    if cur is None or stamp >= cur_stamp:
        latest[name] = c

FAILURES = {
    "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR",
}
PENDING = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "STALE"}

checks = []
pending, failed = [], []
summary = None
for name, c in sorted(latest.items()):
    status = (c.get("status") or c.get("state") or "").upper()
    conclusion = (c.get("conclusion") or "").upper()
    entry = {"name": name, "status": status, "conclusion": conclusion}
    checks.append(entry)
    if name == "CI summary":
        summary = entry
    if status in PENDING or status in ("", "EXPECTED"):
        pending.append(name)
        continue
    if conclusion in FAILURES or status in FAILURES:
        failed.append(name)

ci_conclusion = "unknown"
if failed:
    ci_conclusion = "failure"
elif pending:
    ci_conclusion = "pending"
elif checks:
    ci_conclusion = "success"

reviews = pr.get("reviews") or []
changes_requested = [
    r for r in reviews
    if (r.get("state") or "").upper() == "CHANGES_REQUESTED"
]

out = {
    "repo": repo,
    "pr_number": pr.get("number"),
    "pr_url": pr.get("url"),
    "state": pr.get("state"),
    "is_draft": bool(pr.get("isDraft")),
    "mergeable": pr.get("mergeable"),
    "merge_state_status": pr.get("mergeStateStatus"),
    "branch": pr.get("headRefName"),
    "base": pr.get("baseRefName"),
    "head_sha": head,
    "ci_conclusion": ci_conclusion,
    "ci_summary": summary,
    "checks": checks,
    "pending_checks": pending,
    "failed_checks": failed,
    "changes_requested_count": len(changes_requested),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
PY
