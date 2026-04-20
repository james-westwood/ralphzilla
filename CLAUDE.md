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
| `ralph-loop.sh` | Sprint runner (bash) |

## Non-negotiable constraints

- All implementation code in `ralph.py` — no sub-modules
- No `shell=True` anywhere
- `TaskTracker` exclusively owns `prd.json`
- Never commit directly to `main`
- No AI attribution in commits, PRs, or issues

## Stack

Python 3.13+, click, pytest, ruff (100-char), uv

## Commands

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check ralph.py tests/
```
