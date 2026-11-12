#!/usr/bin/env python3
"""
upstream_check.py — weekly canary for the Goose upstream (b-nnett/goose).

Run by .github/workflows/upstream-watch.yml. Checks three signals:
  1. upstream HEAD moved (repo unarchived + new commits)
  2. archived flag flipped
  3. forks with recent pushes (successor-project candidates)

Compares against .github/upstream-state.json (committed by CI). First run
with no state file = silent baseline. Writes a markdown report and sets
workflow outputs: changed=true/false. Pure stdlib.

Rate limits: unauthenticated GitHub API is 60 req/hr per IP — we make ~3.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

UPSTREAM = "b-nnett/goose"
API = f"https://api.github.com/repos/{UPSTREAM}"
STATE_FILE = ".github/upstream-state.json"
REPORT_FILE = ".github/upstream-report.md"
FORK_WINDOW_DAYS = 30


def get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "whoop-ble-upstream-watch",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def days_since(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 9999


def main():
    os.makedirs(".github", exist_ok=True)
    repo = get(API)
    head = None
    try:
        head = get(f"{API}/commits/{repo['default_branch']}")["sha"]
    except Exception:
        pass
    forks = get(f"{API}/forks?sort=newest&per_page=100")

    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    baseline = not state

    changes = []
    if head and head != state.get("head"):
        changes.append(f"- upstream HEAD moved: `{state.get('head')}` → `{head}` "
                       f"([new commits](https://github.com/{UPSTREAM}/commits)")
    if repo.get("archived") != state.get("archived"):
        flag = "**the upstream was UNARCHIVED**" if repo.get("archived") is False \
            else "upstream became archived"
        changes.append(f"- {flag}")

    known_forks = set(state.get("forks", []))
    fresh = []
    for f in forks:
        if days_since(f.get("pushed_at", "")) <= FORK_WINDOW_DAYS:
            fresh.append(f)
    for f in fresh:
        if f["full_name"] not in known_forks:
            changes.append(
                f"- active fork (successor candidate): "
                f"[{f['full_name']}](https://github.com/{f['full_name']}) "
                f"— pushed {f.get('pushed_at')}, "
                f"{f.get('open_issues_count', '?')} open issues")

    new_state = {
        "head": head,
        "archived": repo.get("archived"),
        "pushed_at": repo.get("pushed_at"),
        "forks": sorted({f["full_name"] for f in fresh} | known_forks)[-100:],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w") as fh:
        json.dump(new_state, fh, indent=1)
        fh.write("\n")

    if changes and not baseline:
        with open(REPORT_FILE, "w") as fh:
            fh.write("Weekly upstream canary found new activity from the Goose "
                     "project (the source of most protocol knowledge in this repo):\n\n")
            fh.write("\n".join(changes) + "\n\n")
            fh.write(f"Upstream: https://github.com/{UPSTREAM}\n\n")
            fh.write("If there are new command numbers, packet decodes, or frame-format "
                     "changes, integrate them via the checklist in this repo's README "
                     "(Appendix for AI agents → *Integrating new upstream findings*).\n")
        print("CHANGED=1")
    else:
        print("CHANGED=0")
        if changes:
            print("(baseline run — state recorded, no notification)")
        print(f"baseline={baseline} head={head} "
              f"archived={repo.get('archived')} active_forks={len(fresh)}")

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as out:
        out.write(f"changed={'true' if changes else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
