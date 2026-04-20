# Claude Session Context — Ralphzilla

## Project Overview

**Name**: Ralphzilla
**Purpose**: AI sprint runner — executes `prd.json` task backlogs via AI agents with autonomous failure recovery
**Repository**: https://github.com/james-westwood/ralphzilla
**Location**: `/home/james/Programming/personal/ralphzilla/`
**Status**: Milestone 1 — Core Loop (under active development)

## What is Ralphzilla?

Ralphzilla is a Python rewrite of `ralph-loop.sh` (~700 lines of fragile bash). It picks tasks from a `prd.json` backlog, codes them via AI agents (Claude, Gemini, opencode), reviews them, waits for CI, and merges — autonomously handling recoverable failures.

See `ralph-py-rewrite-plan.md` in the MasterVault for the full design.
See `roadmap.md` for the milestone plan.

## Technology Stack

- Python 3.13+ with native type hints
- `click` for CLI
- `pytest` for testing (≥70% coverage target)
- `ruff` for linting and formatting (100-char line length)
- `uv` for package management
- Single-file: all code lives in `ralph.py`

## Project Structure

```
ralphzilla/
├── ralph.py                    # Entire implementation (single file)
├── prd.json                    # Task backlog
├── pyproject.toml              # Package metadata
├── CLAUDE.md                   # This file
├── CODER_INSTRUCTIONS.md       # AI coder instructions
├── REVIEWER_INSTRUCTIONS.md    # AI reviewer instructions
├── README.md                   # User docs
├── roadmap.md                  # Milestone plan
├── tests/                      # Test suite
└── .github/workflows/ci.yml    # CI pipeline
```

## Development Workflow

### Setup
```bash
cd /home/james/Programming/personal/ralphzilla
uv venv --python /usr/bin/python3
uv sync --extra dev
```

### Common Commands
```bash
uv run pytest tests/ -v
uv run ruff check ralph.py tests/
uv run ruff format ralph.py tests/
uv run python ralph.py --help
```

## Feature Branching Policy

**Never commit directly to `main`. Every change goes through a feature branch + PR.**

### Branch naming
| Type | Pattern | Example |
|---|---|---|
| Feature | `feature/<issue-number>-<slug>` | `feature/1-task-tracker` |
| Fix | `fix/<issue-number>-<slug>` | `fix/7-ci-stale-race` |

### Before writing any code

1. Create a GitHub issue (`gh issue create`)
2. Create a feature branch
3. Write tests first (TDD)
4. Open PR when passing

### PR checklist
- [ ] All tests pass: `uv run pytest tests/ -v`
- [ ] Ruff clean: `uv run ruff check ralph.py tests/`
- [ ] No `shell=True` anywhere in `ralph.py`
- [ ] No magic numbers — use named constants from the `Constants` block
- [ ] Type hints on all public functions

## Critical Design Constraints

These are non-negotiable — do not violate them:

1. **Single file**: All implementation code in `ralph.py`. No sub-modules, no packages.
2. **No `shell=True`**: All subprocess calls via `SubprocessRunner` with list args.
3. **`TaskTracker` exclusively owns `prd.json`**: No other class reads or writes it.
4. **`PRDGuard` threshold is 0**: Any mutation of `prd.json` by the coder is a violation.
5. **Fresh load on every write**: `mark_complete()` always does `json.load()` — never uses cached state.
6. **SSH remote enforcement**: Every push is preceded by `verify_ssh_remote()`.
7. **`reset --hard` not `pull --ff-only`**: `ensure_main_up_to_date()` uses fetch + reset --hard.
8. **JSON CI parsing**: Use `conclusion` field from `gh pr checks --json`, not `state`.

## No AI Attribution

Never mention Claude, AI, or any AI tool in commit messages, PR titles, PR bodies, issue comments, or any other Git/GitHub content. Write all such content as the developer.

## Related Documentation

- **Full design**: `/home/james/MasterVault/30_Personal_Projects/Programming/RaplhZilla/ralph-py-rewrite-plan.md`
- **Roadmap**: `/home/james/Programming/personal/ralphzilla/roadmap.md`
- **Lessons learned**: In the rewrite plan, sections "Contributions from Production Experience"
