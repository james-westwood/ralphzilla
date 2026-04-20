# Coder Instructions — Ralphzilla

You are a Python developer working on Ralphzilla, an AI sprint runner. Claude is the senior developer and will review your PRs. Do not review code you have written yourself.

---

## Project Overview

Ralphzilla executes `prd.json` task backlogs via AI agents with autonomous failure recovery. It is a single Python file (`ralph.py`) that manages the full loop: branch → code → pre-commit → test → PR → review → CI → merge.

---

## Critical constraints — read these first

These are non-negotiable. Violating any of them is a blocking review issue.

1. **Single file**: All code goes in `ralph.py`. No sub-modules, no packages, no imports from other local files.
2. **No `shell=True`**: All subprocess calls use list args. Never `subprocess.run("cmd arg", shell=True)`.
3. **`TaskTracker` exclusively owns `prd.json`**: No other class reads or writes it directly.
4. **`PRDGuard` threshold is 0**: Any mutation of `prd.json` in a PR diff is a violation — close the PR.
5. **`mark_complete()` fresh load**: Always `json.load()` from disk — never use cached state.
6. **`reset --hard` not `pull --ff-only`**: `ensure_main_up_to_date()` uses `git fetch && git reset --hard origin/main`.
7. **JSON CI parsing**: Always use `conclusion` field from `gh pr checks --json`, not `state`.
8. **Do NOT touch `prd.json` or `progress.txt`**: The orchestrator handles those after your PR is merged.

---

## Tech stack

- **Python**: 3.13+ with native type hints (`list[str]`, `dict[str, int]`, `str | None`)
- **Package manager**: uv (`uv run pytest`, `uv run ruff`, etc.)
- **Testing**: pytest, `tmp_path` fixture for file I/O
- **Linting/formatting**: ruff (100-char line length)
- **CLI**: click
- **No external dependencies** beyond `click` — stdlib only otherwise

---

## Code standards

- All public functions must have complete type hints
- No magic numbers — define named constants in the `Constants` block at the top of `ralph.py`
- Use `@dataclass` for data containers, not plain dicts
- Don't swallow exceptions silently — log them and re-raise or convert to a typed `RalphError`
- Functions >50 lines or nesting >4 levels should be split
- Tests must cover the acceptance criteria from the task, not just "it runs without error"
- Use `tmp_path` for any file I/O in tests — no hardcoded paths

---

## Workflow

### Before writing any code
1. `gh issue create` — create a GitHub issue with title, body, and labels
2. `git checkout -b feature/<issue-number>-<slug>` — create feature branch off main
3. Never commit directly to main

### Development loop
```bash
uv run pytest tests/ -v              # run tests
uv run ruff check ralph.py tests/    # lint
uv run ruff format ralph.py tests/   # format
uv run pre-commit run --all-files    # all hooks
```

### Opening a PR
```bash
git push -u origin <branch>
gh pr create --title "type(scope): description" --body "Closes #<issue>"
```

### Git identity
```bash
git config user.name "Gemini"
git config user.email "gemini-cli@google.com"
```

---

## `ralph.py` structure

Top-to-bottom definition order (do not reorder):

1. Shebang + module docstring
2. Imports (stdlib → click)
3. Constants block
4. Exception classes
5. Dataclasses
6. `RalphLogger`
7. `SubprocessRunner`
8. `TaskTracker`
9. `PlanChecker`
10. `BranchManager`
11. `PRManager`
12. `PRDGuard`
13. `PromptBuilder`
14. `AIRunner`
15. `PreCommitGate`
16. `TestRunner`
17. `ReviewLoop`
18. `CIPoller`
19. `Orchestrator`
20. CLI entry point (click)
21. `main()`
22. `if __name__ == "__main__": sys.exit(main())`
