# Ralphzilla

AI sprint runner — executes `prd.json` backlogs via AI agents with autonomous failure recovery.

**Repo**: https://github.com/james-westwood/ralphzilla
**Location**: `/home/james/Programming/personal/ralphzilla/`

## Key files

| File | Purpose |
|---|---|
| `DESIGN.md` | Full architecture, class specs, constraints, lessons learned — read before touching code |
| `SCRUM_MASTER.md` | How to run the ralph loop, monitor it, and recover from failures — read before starting a sprint |
| `roadmap.md` | Milestone plan |
| `prd.json` | Task backlog |
| `ralph.py` | The entire implementation (single file) |

## Non-negotiable constraints

- All implementation code in `ralph.py` — no sub-modules
- No `shell=True` anywhere
- `TaskTracker` exclusively owns `prd.json`
- Never commit directly to `main`
- No AI attribution in commits, PRs, or issues
- **Commit before ruff.** After editing a file, commit it immediately with `--no-verify` before running `ruff format` or any tool that touches the file. Ruff can silently discard uncommitted edits. If you commit first, `git checkout -- <file>` recovers anything lost. This rule applies to scrum masters and coders alike.

## Stack

Python 3.13+, click, pytest, ruff (100-char), uv

## Commands

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check ralph.py tests/
```

## GitHub workflow

- Request Copilot PR review: `gh pr edit PR-NUMBER --add-reviewer @copilot`
- Always commit with `--no-verify` — the pre-commit hook switches HEAD to main, losing the feature branch. Run `uv run ruff check` and `uv run pytest` manually instead.
- After committing on the wrong branch, cherry-pick to the correct branch and reset the wrong one: `git checkout feat/X && git cherry-pick SHA && git branch -f main <clean-sha>`
