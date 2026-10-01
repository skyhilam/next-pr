#!/usr/bin/env bash
# Observe PR / head / CI facts for skyhilam/dkdm-monorepo (read-only).
# Use this for the Observe step; write the JSON into the work log before Act.
#
# Usage:
#   check-pr.sh <pr-number-or-url>
#
# Prints JSON on stdout with fields bound to the current head SHA.
set -euo pipefail

REPO="${NEXT_PR_REPO:-skyhilam/dkdm-monorepo}"
TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "usage: check-pr.sh <pr-number-or-url>" >&2
  exit 2
fi

PR_JSON="$(gh pr view "$TARGET" --repo "$REPO" --json \
  number,url,title,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName,statusCheckRollup,reviews,comments)"

python3 - <<'PY' "$PR_JSON"
import json, sys
from collections import defaultdict

pr = json.loads(sys.argv[1])
head = pr.get("headRefOid") or ""

# Latest check per name (by completedAt/startedAt)
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
pending, failed, success = [], [], []
summary = None
for name, c in sorted(latest.items()):
    status = (c.get("status") or c.get("state") or "").upper()
    conclusion = (c.get("conclusion") or "").upper()
    entry = {"name": name, "status": status, "conclusion": conclusion}
    checks.append(entry)
    if name == "CI summary":
        summary = entry
    if status in PENDING or (not conclusion and status not in ("COMPLETED", "SUCCESS", "FAILURE")):
        if status in PENDING or status in ("", "EXPECTED"):
            pending.append(name)
            continue
    if conclusion in FAILURES or status in FAILURES:
        failed.append(name)
    elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") or status in ("SUCCESS",):
        success.append(name)
    elif status == "COMPLETED" and conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED", ""):
        success.append(name)
    elif conclusion == "" and status == "COMPLETED":
        success.append(name)

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
