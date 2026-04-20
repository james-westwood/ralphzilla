# Ralphzilla

> AI sprint runner — executes `prd.json` task backlogs via AI agents with autonomous failure recovery.

**Status**: Under active development (Milestone 1 — Core Loop)

---

## What it does

Give Ralphzilla a `prd.json` backlog and it will:

1. Pick the next incomplete task
2. Create a feature branch
3. Invoke an AI coder (Claude, Gemini, or opencode)
4. Run pre-commit hooks and tests, fixing failures automatically
5. Open a PR, run a reviewer, and respond to change requests
6. Wait for CI, fixing failures autonomously up to a configurable limit
7. Merge, update `prd.json`, and move to the next task

Recoverable failures (CI flakes, reviewer change requests, pre-commit issues) are handled without human intervention. Unrecoverable failures escalate cleanly.

---

## Installation

```bash
pipx install ralphzilla   # coming in Milestone 3
```

Or copy `ralph.py` directly into your project:

```bash
curl -O https://raw.githubusercontent.com/james-westwood/ralphzilla/main/ralph.py
chmod +x ralph.py
./ralph.py --help
```

---

## Quickstart

```bash
# Initialise a project (coming in Milestone 3)
ralph init

# Run the sprint
ralph --max 10

# Run a specific task
ralph --task TASK-ID

# Dry run (no AI calls or git ops)
ralph --dry-run
```

---

## `prd.json` format

```json
{
  "project": "my-project",
  "quality_checks": ["uv run pytest", "uv run ruff check ."],
  "epic_addenda": {
    "GUI": "Check for framework runtime API errors."
  },
  "tasks": [
    {
      "id": "FEAT-01",
      "epic": "FEAT",
      "title": "implement_user_auth",
      "owner": "ralph",
      "description": "Add JWT-based authentication to the API.",
      "acceptance_criteria": [
        "POST /auth/login returns a signed JWT on valid credentials",
        "Protected routes return 401 without a valid token",
        "Tests cover happy path, invalid credentials, and expired token"
      ],
      "files": ["src/auth.py", "tests/test_auth.py"],
      "completed": false,
      "priority": 1
    }
  ]
}
```

---

## Roadmap

See [roadmap.md](roadmap.md) for the full milestone plan.

| Milestone | Status |
|---|---|
| M1 — Core Loop | 🚧 In progress |
| M2 — Quality & Reliability | ⬜ Planned |
| M3 — Distribution & DX | ⬜ Planned |
| M4 — Scrum Master Layer | ⬜ Planned |
| M5 — Parallelism & Scale | ⬜ Planned |
| M6 — Ecosystem & Maturity | ⬜ Planned |

---

## Architecture

Single-file Python (`ralph.py`). No package, no pip install beyond what's in the project venv. Copy it in or install via `pipx`.

Key classes: `Orchestrator`, `TaskTracker`, `PlanChecker`, `BranchManager`, `PRManager`, `AIRunner`, `PromptBuilder`, `PreCommitGate`, `TestRunner`, `ReviewLoop`, `CIPoller`, `PRDGuard`.

See `ralph-py-rewrite-plan.md` for full design documentation.

---

## License

MIT
