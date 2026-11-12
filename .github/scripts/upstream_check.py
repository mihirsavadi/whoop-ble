#!/usr/bin/env python3
"""
upstream_check.py — weekly canary for both upstreams of this repo's
protocol knowledge.

  b-nnett/goose  (ARCHIVED): watch HEAD movement, archived-flag flip, and
                  forks with recent pushes (successor candidates).
  ryanbr/noop    (ACTIVE):   watch releases (the integration points);
                  HEAD is recorded for reference but does not notify —
                  an active repo moves every week.

Run by .github/workflows/upstream-watch.yml. Compares against
.github/upstream-state.json (committed by CI). First run with no state
file = silent baseline. Writes .github/upstream-report.md and sets
workflow output changed=true/false when anything noteworthy moved.
Pure stdlib. ~6 unauthenticated GitHub API calls (limit 60/hr).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

UPSTREAMS = {
    "b-nnett/goose": {"mode": "archived", "watch_forks": True},
    "ryanbr/noop": {"mode": "active", "watch_forks": False},
}
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


def check_repo(full_name, config, old):
    """Return (new_state_dict, changes_list)."""
    api = f"https://api.github.com/repos/{full_name}"
    repo = get(api)
    head = None
    try:
        head = get(f"{api}/commits/{repo['default_branch']}")["sha"]
    except Exception:
        pass

    changes = []
    new = {"head": head, "archived": repo.get("archived"),
           "pushed_at": repo.get("pushed_at")}

    # archived-mode repos: HEAD movement and archived flip both notify
    if config["mode"] == "archived":
        if head and head != old.get("head"):
            changes.append(f"- `{full_name}` HEAD moved: `{old.get('head')}` → "
                           f"`{head}` ([commits](https://github.com/{full_name}/commits)")
        if repo.get("archived") != old.get("archived"):
            flag = ("**UNARCHIVED** — the dormant upstream is alive again"
                    if repo.get("archived") is False else "became archived")
            changes.append(f"- `{full_name}` {flag}")
        if config.get("watch_forks"):
            forks = get(f"{api}/forks?sort=newest&per_page=100")
            fresh = [f for f in forks
                     if days_since(f.get("pushed_at", "")) <= FORK_WINDOW_DAYS]
            known = set(old.get("forks", []))
            for f in fresh:
                if f["full_name"] not in known:
                    changes.append(
                        f"- active fork (successor candidate): "
                        f"[{f['full_name']}](https://github.com/{f['full_name']}) "
                        f"— pushed {f.get('pushed_at')}")
            new["forks"] = sorted({f["full_name"] for f in fresh} | known)[-100:]
    else:
        # active repos: releases are the integration points
        release = None
        try:
            release = get(f"{api}/releases/latest")
        except Exception:
            pass
        new["release"] = release and {
            "tag": release.get("tag_name"),
            "published_at": release.get("published_at"),
            "url": release.get("html_url"),
        }
        if new["release"] and new["release"] != old.get("release"):
            r = new["release"]
            was = (old.get("release") or {}).get("tag")
            changes.append(
                f"- `{full_name}` new release: **{r['tag']}** "
                f"(was {was or 'none'}) — {r['url']}")
        if repo.get("archived") != old.get("archived"):
            changes.append(f"- `{full_name}` archived flag changed → "
                           f"{repo.get('archived')}")

    return new, changes


def main():
    state = {"repos": {}}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    old_repos = state.get("repos", {})
    baseline = not old_repos

    all_changes = []
    for full_name, config in UPSTREAMS.items():
        try:
            new, changes = check_repo(full_name, config,
                                      old_repos.get(full_name, {}))
            old_repos[full_name] = new
            all_changes.extend(changes)
        except Exception as ex:
            all_changes.append(f"- `{full_name}` check failed: {ex}")

    state = {"repos": old_repos,
             "checked_at": datetime.now(timezone.utc).isoformat()}
    os.makedirs(".github", exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")

    if all_changes and not baseline:
        with open(REPORT_FILE, "w") as fh:
            fh.write("Weekly upstream canary found new activity:\n\n")
            fh.write("\n".join(all_changes) + "\n\n")
            fh.write("New protocol knowledge may be available. Integration "
                     "checklist: this repo's README (Appendix for AI agents → "
                     "*Integrating new upstream findings*).\n")
        print("CHANGED=1")
    else:
        print("CHANGED=0")
        if all_changes:
            print("(baseline run — state recorded, no notification)")
        for name, s in old_repos.items():
            print(f"  {name}: head={str(s.get('head'))[:12]} "
                  f"release={(s.get('release') or {}).get('tag')} "
                  f"archived={s.get('archived')}")

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as out:
        out.write(f"changed={'true' if all_changes and not baseline else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
