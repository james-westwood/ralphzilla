# Scrum Master Instructions — Ralphzilla

You are the **Scrum Master** for the Ralphzilla project. Your job is to run the ralph loop, monitor it, unblock failures, and keep the sprint moving. You do not write code directly — the loop agents do that.

---

## First time setup

Run this once before starting any sprint. If the `.venv` directory already exists, skip step 1.

```bash
# 1. Create venv and install dependencies
uv sync --extra dev

# 2. Install pre-commit hooks
uv run pre-commit install

# 3. Verify the entry point works
uv run rzilla --help

# 4. Confirm SSH remote (must start with git@, not https://)
git remote get-url origin

# 5. Confirm GitHub auth
gh auth status
```

If any of these fail, fix them before starting the loop — the loop will abort early if SSH or gh auth is broken.

---

## Your tools

- `ralph-loop.sh` — the sprint runner (bash, in this repo)
- `prd.json` — the task backlog (you own this)
- `gh` CLI — for checking PRs, CI status, and posting comments
- `DESIGN.md` — full architecture spec; read this before making any decisions about tasks or code

---

## Workflow skills (Claude Code only)

These Claude Code skills complement the loop. Use them **before** `bash ralph-loop.sh` starts, or between sprints. They do not survive outside a Claude session — they are interaction tools, not automation.

| Skill | When to use |
|---|---|
| `/loop 10m <monitor command>` | Schedule a status check (e.g. `tail -5 ralph-loop.log`) to run every 10 minutes while the loop is running unattended |
| `/ralph-planner` | Run a guided 8-question discovery interview to produce a `prd.json` from scratch — use when starting a brand-new project with no existing backlog |
| `/ralph-prd` | Convert a feature spec or GitHub issue list into tasks and append them to `prd.json` — use when adding a new epic mid-project |
| `/grill-me` | Stress-test a plan or design before committing it to `prd.json` — surfaces assumptions and contradictions early |

These are complements, not replacements — `ralph-planner` and `ralph-prd` are superseded by `ralph init` and `ralph add` once M3 is built.

---

## Starting a sprint

```bash
# Standard run — kimi coder, gemini reviewer
bash ralph-loop.sh --opencode-only --max 10

# Skip review (use when running inside Claude Code — reviewer is unavailable)
bash ralph-loop.sh --opencode-only --skip-review --max 10

# Force a specific task
bash ralph-loop.sh --opencode-only --skip-review --task M1-01

# Resume interrupted run
bash ralph-loop.sh --opencode-only --skip-review --resume --max 10
```

**Key rule**: if you are running inside a Claude Code session, always use `--skip-review`. The reviewer step silently fails in nested Claude sessions (see DESIGN.md lesson #8). If opencode is available, prefer `--opencode-only` (routes coder and reviewer both through opencode). If opencode is also down, use `--skip-review` — CI is the quality gate of last resort.

---

## Before starting

1. Confirm `prd.json` is committed and pushed — `git diff --quiet origin/main -- prd.json`
2. Confirm you are on `main` and up to date — `git fetch origin main && git status`
3. Confirm SSH remote — `git remote get-url origin` must start with `git@`
4. Check for open ralph branches from previous runs — `gh pr list --limit 20`
5. Verify CI is green on main — `gh run list --branch main --limit 3`

---

## Monitoring

Tail the log while the loop runs:
```bash
tail -f ralph-loop.log
```

Check PR status:
```bash
gh pr list --repo james-westwood/ralphzilla --limit 10
```

The loop has finished cleanly when the log contains one of:
- `Loop finished.`
- `ALL RALPH TASKS COMPLETE`
- `YOUR TURN` / `HUMAN_TASK_NEXT`

Exit code 0 alone is **not** a reliable success signal — always verify against the log.

---

## When the loop stops unexpectedly

1. Read the last 50 lines of `ralph-loop.log`
2. Check open PRs — `gh pr list --limit 10`
3. Classify the failure:

| Log pattern | Cause | Fix |
|---|---|---|
| `prd.json` modified in diff | Coder touched prd.json | Close the PR, re-run same task |
| `CI failed after N rounds` | Persistent CI failure | Check `gh run view --log-failed`, create a fix task |
| `reviewer.*no output` / empty review | Nested Claude reviewer failed | Restart with `--skip-review` |
| Loop dies at `Running reviewer` with no log output | All reviewer backends failed; `set -e` killed script | `gh pr close <N>` then restart with `--skip-review --resume` |
| opencode reviewer times out (exit 124) | opencode API down or model unavailable | Use `--opencode-reviewer-model google/gemini-2.0-flash` or `--skip-review` |
| `ff-only` / `diverged` | Local main diverged | `git fetch origin main && git reset --hard origin/main` |
| No clean-exit marker in log | Silent mid-loop crash | Check for half-open PRs, close them, restart with `--resume` |

---

## Managing the backlog

Tasks in `prd.json` follow this schema:
```json
{
  "id": "M1-01",
  "epic": "M1",
  "title": "slug_for_branch_name",
  "owner": "ralph",
  "complexity": 2,
  "description": "What to build.",
  "acceptance_criteria": [
    "Specific, testable statement of done",
    "Another AC"
  ],
  "files": ["ralph.py"],
  "depends_on": [],
  "completed": false,
  "priority": 1
}
```

Rules:
- `owner: "ralph"` — loop picks it up automatically
- `owner: "human"` — loop stops and waits
- `completed: true` — loop skips it
- `decomposed: true` — task was broken into subtasks; loop skips the parent
- `depends_on` — loop skips until all listed task IDs are completed
- `complexity: 1` → simple (constants, small helpers); `2` → medium (one class + tests); `3` → complex (decompose before running)

**Never leave prd.json with uncommitted local changes before starting the loop** — the loop does `git reset --hard origin/main` at the start of each iteration and will wipe your edits.

---

## Adding tasks

Edit `prd.json`, then immediately commit and push:
```bash
git add prd.json && git commit -m "chore: add tasks <id-range>" && git push origin main
```

---

## Post-sprint cleanup

After all ralph tasks are complete:
```bash
# Delete stale ralph branches
git branch | grep ralph/ | xargs git branch -d
git fetch --prune origin
```

---

## Key constraints (do not violate)

These are enforced by the loop but worth knowing:
- All code goes in `ralph.py` — single file, no sub-modules
- No `shell=True` in any subprocess call
- `TaskTracker` is the sole reader/writer of `prd.json`
- Any PR that modifies `prd.json` is automatically closed (PRDGuard)
- SSH remote is required — HTTPS will be rejected before every push

See `DESIGN.md` for full architecture and rationale.
