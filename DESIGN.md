# ralph.py — Rewrite Plan

**Status**: Planning
**Date**: 2026-03-15
**Context**: Rewriting `ralph-loop.sh` (~700 lines bash) as a structured Python script after hitting several production issues running the loop against `playchitect`.

---

## Why Python?

The bash version has grown to ~700 lines and has accumulated a series of fragile workarounds:

- Grep-based CI status parsing (breaks if GitHub changes output format)
- Prompt injection via heredocs (quoting bugs, `ARG_MAX` limits)
- AI agents corrupting `prd.json` (kimi-k2.5 bulk-marked all 30 tasks complete in one run)
- `--auto` merge not available on repos with branch protection
- HTTPS remote failing silently on non-interactive pushes
- No real error hierarchy — everything either `exit 1` or `|| true`

Python gives us proper data structures, exception handling, subprocess safety, and testability.

---

## Architecture

Single file: `ralph.py`. No package, no pip install beyond what's in the project venv. Run as `./ralph.py [OPTIONS]`.

```
Orchestrator
├── TaskTracker          — sole owner of prd.json / progress.txt; add_task() for decomposition
├── PlanChecker          — pre-sprint validation: schema, ACs, dependencies, AI sanity check,
│                          complexity inference, auto-decomposition of complex tasks
├── BranchManager        — all git operations
├── PRManager            — all gh pr operations
├── AIRunner             — claude / gemini / opencode subprocess wrappers; complexity-based routing
├── PromptBuilder        — all prompt templates (stateless, pure functions)
├── PreCommitGate        — runs pre-commit, invokes coder fix loop on failure
├── TestRunner           — runs quality_checks from prd.json, invokes coder fix loop on failure
├── TestWriter           — TDD mode: separate agent writes failing tests before coder starts
├── TestQualityChecker   — validates tests aren't hollow (AST + AI tier); retries TestWriter
├── ReviewLoop           — reviewer + coder fix loop (CHANGES REQUESTED)
├── ReviewQualityChecker — validates review is substantive (deterministic + AI tier)
├── CIPoller             — polls CI, fetches failure logs, invokes coder fix loop
├── PRDGuard             — pre-merge check: did coder touch prd.json?
└── SubprocessRunner     — shared subprocess wrapper with timeout + env control
```

---

## Constants

```python
DEFAULT_MAX_ITERATIONS       = 10
DEFAULT_MAX_PRECOMMIT_ROUNDS = 2
DEFAULT_MAX_REVIEW_ROUNDS    = 2
DEFAULT_MAX_CI_FIX_ROUNDS    = 2
DEFAULT_MAX_TEST_FIX_ROUNDS  = 2
CI_POLL_INTERVAL_SECS        = 30
CI_POLL_MAX_ATTEMPTS         = 60   # 30 min total
CI_PENDING_STATES = frozenset({"PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "EXPECTED"})
CI_FAILURE_STATES = frozenset({"FAILURE", "ERROR"})
SUBPROCESS_TIMEOUT_SECS      = 3600  # 1 hour — AI coder calls can be slow
GH_TIMEOUT_SECS              = 60
GIT_TIMEOUT_SECS             = 120
MAIN_BRANCH                  = "main"
LOG_FILE_NAME                = "ralph.log"
PRD_FILE                     = "prd.json"
PROGRESS_FILE                = "progress.txt"
DEFAULT_OPENCODE_MODEL       = "opencode/kimi-k2.5"
GEMINI_MODEL                 = "gemini-2.5-pro"
```

---

## Exception Hierarchy

```python
class RalphError(Exception): pass
class BranchSyncError(RalphError): pass      # ff-only pull failed (diverged main)
class BranchExistsError(RalphError): pass    # branch exists, resume=False
class RemoteNotSSHError(RalphError): pass    # HTTPS remote detected
class CITimeoutError(RalphError): pass       # CI didn't finish in 30 min
class CIFailedFatal(RalphError): pass        # CI still failing after max fix rounds
class PRDGuardViolation(RalphError): pass    # coder touched prd.json
class CoderFailedError(RalphError): pass     # all coder fallbacks exhausted
class ReviewerFailedError(RalphError): pass  # all reviewer fallbacks exhausted
class PreflightError(RalphError): pass       # missing CLI tool or auth failure
class PlanInvalidError(RalphError): pass     # plan-checker found structural violations
```

---

## Data Classes

```python
@dataclass class Config:
    max_iterations: int
    skip_review: bool
    tdd_mode: bool           # per-sprint TDD flag (--tdd); test writer ≠ coder agent
    model_mode: str          # "random" | "claude" | "gemini" | "opencode"
    opencode_model: str
    resume: bool
    repo_dir: Path
    log_file: Path
    max_precommit_rounds: int
    max_review_rounds: int
    max_ci_fix_rounds: int
    max_test_fix_rounds: int
    max_test_write_rounds: int   # TDD: rounds to get hollow-free tests
    force_task_id: str | None

@dataclass class PRInfo:
    number: int
    url: str

@dataclass class CIResult:
    passed: bool
    rounds_used: int

@dataclass class ReviewResult:
    verdict: str             # "APPROVED" | "CHANGES_REQUESTED_MAX_REACHED"
    rounds_used: int

@dataclass class PreCommitResult:
    passed: bool
    rounds_used: int

@dataclass class TestResult:
    passed: bool
    rounds_used: int

@dataclass class BranchStatus:
    existed: bool
    had_commits: bool

@dataclass class TaskResult:
    fatal: bool
    message: str = ""

@dataclass class PlanCheckResult:
    valid: bool
    errors: list[str]          # structural violations (block sprint start)
    warnings: list[str]        # AI-flagged issues (log but don't block)
    tasks_checked: int
    decompositions: int        # number of tasks auto-decomposed into subtasks

@dataclass class TestQualityResult:
    passed: bool
    hollow_tests: list[str]    # test names that failed quality checks
    deterministic_issues: list[str]  # ast-detected problems
    ai_issues: list[str]       # AI-flagged semantic hollowness
    rounds_used: int

@dataclass class ReviewQualityResult:
    acceptable: bool
    reason: str                # why it failed quality check (if it did)
```

---

## Class Responsibilities

### `RalphLogger`
Dual-stream logger (stdout + log file). Fixed-width level prefix `[INFO ]` `[WARN ]` `[ERROR]` `[FATAL]`. No Python `logging` module — direct print + file append. Optional `rich` integration detected at runtime for coloured output.

### `SubprocessRunner`
Single wrapper around `subprocess.run()`. Key feature: `env_removals: list[str]` parameter strips env vars before the child process runs (needed for `env -u CLAUDECODE` when calling Claude). All calls logged. Returns `CompletedProcess`. Never `shell=True`.

### `TaskTracker`
**Only class that reads or writes `prd.json` or `progress.txt`.**

```python
def load(self) -> dict
def get_next_task(self) -> dict | None   # first incomplete non-human non-blocked
def get_task_by_id(self, task_id: str) -> dict | None
def count_remaining(self) -> int
def get_quality_checks(self) -> list[str]
def mark_complete(self, task_id: str) -> None   # fresh load every call
def append_progress(self, task_id: str, title: str, pr_number: int, today: str) -> None
def commit_tracking(self, task_id: str, title: str) -> None
    # git add prd.json progress.txt
    # git commit -m "[{task_id}] {title}: mark complete"
    # git push origin main
```

`mark_complete()` always does a fresh `json.load()` — never uses cached state.

### `PlanChecker`

Runs once at sprint start, before any git operations. Two-tier validation:

**Tier 1 — Structural (always runs, blocks on failure):**

```python
def check_structural(self, prd: dict) -> list[str]:
    errors = []
    required_fields = {"id", "title", "description", "acceptance_criteria", "owner", "completed"}
    all_ids = {t["id"] for t in prd["tasks"]}

    for task in prd["tasks"]:
        if task.get("completed"):
            continue
        missing = required_fields - task.keys()
        if missing:
            errors.append(f"{task['id']}: missing fields {missing}")
        if not task.get("acceptance_criteria"):
            errors.append(f"{task['id']}: acceptance_criteria is empty")
        for dep in task.get("depends_on", []):
            dep_task = next((t for t in prd["tasks"] if t["id"] == dep), None)
            if dep_task is None:
                errors.append(f"{task['id']}: depends_on unknown task '{dep}'")
            elif not dep_task.get("completed"):
                errors.append(f"{task['id']}: depends_on incomplete task '{dep}'")
    return errors
```

Raises `PlanInvalidError` if any structural errors found. The sprint does not start.

**Tier 2 — AI sanity check (opt-in via `--validate-plan`, produces warnings only):**

Sends the incomplete task list to an AI with a prompt asking it to flag:
- Tasks whose acceptance criteria are untestable ("it works", "looks good")
- Tasks that are not atomic (two or more distinct deliverables in one task)
- Tasks whose description contradicts the acceptance criteria
- Tasks that are ambiguous about what files/modules to touch

AI output is parsed for `[WARN]` lines and logged. Does **not** block the sprint — the operator decides whether to act on warnings. This is the GSD plan-checker pattern: validate goal achievability before execution begins, but keep the human in control of the final call.

```python
def check_ai(self, tasks: list[dict]) -> list[str]:
    prompt = PromptBuilder.plan_check_prompt(tasks)
    output = self.ai_runner.run_reviewer("claude", prompt)
    return re.findall(r'^\[WARN\] .+', output, re.MULTILINE)

def run(self, prd: dict, ai_check: bool = False) -> PlanCheckResult:
    errors = self.check_structural(prd)
    warnings = self.check_ai(prd["tasks"]) if ai_check and not errors else []
    decompositions = self.auto_decompose(prd) if not errors else 0
    return PlanCheckResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        tasks_checked=sum(1 for t in prd["tasks"] if not t.get("completed")),
        decompositions=decompositions,
    )
```

**Tier 3 — Complexity inference and auto-decomposition:**

Every incomplete task gets a complexity score (1–3) via two sources:
- **Author-set**: `"complexity": 2` field in prd.json — respected as-is
- **Inferred**: if no `complexity` field, PlanChecker scores it based on: AC count (>4 → +1), description word count (>80 → +1), `files` count (>3 → +1), keywords ("refactor", "migrate", "redesign" → +1). Clamped to 1–3.

If a task scores complexity=3 and has no `parent` field (i.e. it hasn't already been decomposed), `auto_decompose()` fires:

```python
def auto_decompose(self, prd: dict) -> int:
    """Break complexity-3 tasks into labelled subtasks a/b/c via AI.
    Writes subtasks back via TaskTracker. Returns count of decompositions."""
    count = 0
    for task in prd["tasks"]:
        if task.get("completed") or task.get("decomposed") or task.get("parent"):
            continue
        complexity = task.get("complexity") or self._infer_complexity(task)
        if complexity < 3:
            continue
        subtasks = self._ai_decompose(task)   # returns list[dict] of subtask dicts
        for i, sub in enumerate(subtasks):
            sub["id"] = f"{task['id']}{chr(ord('a') + i)}"   # FEAT-01a, FEAT-01b, …
            sub["parent"] = task["id"]
            sub["complexity"] = 2             # subtasks start at medium; re-scored next sprint
            if i > 0:
                sub["depends_on"] = [f"{task['id']}{chr(ord('a') + i - 1)}"]
            self.task_tracker.add_task(sub)
        self.task_tracker.mark_decomposed(task["id"])
        count += 1
    return count
```

`mark_decomposed()` sets `"decomposed": true` on the parent task — `TaskTracker.get_next_task()` skips decomposed tasks. The orchestrator logs which tasks were broken down before the sprint starts, so the operator can review before proceeding (or pass `--no-decompose` to skip Tier 3).

### `BranchManager`
All git operations. Critical methods:

- `verify_ssh_remote()` — hard preflight check; `git remote get-url origin` must start with `git@`
- `ensure_main_up_to_date()` — `git checkout main && git pull --ff-only origin main`; raises `BranchSyncError` on diverge (never silently ignores)
- `checkout_or_create(branch, resume)` — handles fresh vs resume paths
- `push_branch(branch)` — calls `verify_ssh_remote()` before every push

### `PRManager`
All `gh pr` operations. Parses PR number with `re.search(r'(\d+)$', url)` — not grep.

- `get_diff(pr_number, retries=5, delay=10)` — retries with delay for race condition on fresh PRs
- `get_diff_for_file(pr_number, filepath)` — used by `PRDGuard`
- `close(pr_number, reason)` — posts reason as comment before closing

### `PRDGuard`
Pre-merge safety check. Diffs the PR against `prd.json`. Uses:
```python
re.findall(r'^\+.*"completed":\s*true', diff, re.MULTILINE)
```
Threshold: `> 0` (not `> 1` as in bash). The coder is told not to touch `prd.json` at all — any modification is a violation. Raises `PRDGuardViolation`, which causes the PR to be closed and the loop to abort.

### `PromptBuilder`
Stateless class, pure static methods. All prompt text lives here, not in control-flow code.

```python
@staticmethod def coder_prompt(task, coder, resume=False) -> str
@staticmethod def precommit_fix_prompt(task, precommit_output) -> str
@staticmethod def test_fix_prompt(task, test_output) -> str
@staticmethod def reviewer_prompt(task, coder, reviewer, diff, round_num, prd) -> str
@staticmethod def review_fix_prompt(task, review_text) -> str
@staticmethod def ci_fix_prompt(task, failure_log) -> str
@staticmethod def pr_body(task, coder, reviewer) -> str
@staticmethod def plan_check_prompt(tasks) -> str
@staticmethod def test_writer_prompt(task) -> str        # TDD: write failing tests for these ACs
@staticmethod def test_quality_prompt(task, test_file_contents, ast_report) -> str  # AI tier
@staticmethod def decompose_prompt(task) -> str          # break complexity-3 task into subtasks
```

The coder prompt explicitly states: *"Do NOT touch prd.json or progress.txt — the orchestrator handles all of that after your PR is merged."*

`reviewer_prompt()` must instruct the reviewer to evaluate the diff against the following six categories (derived from the `code-reviewer` skill checklist):

1. **Correctness** — logic errors, edge cases, data handling
2. **Security** — hardcoded secrets, injection, input validation
3. **Performance** — N+1 queries, unbounded collections
4. **Maintainability** — functions >50 lines, nesting >4 levels, magic numbers
5. **Testing** — acceptance criteria from the task are covered; no implementation-testing
6. **PRD adherence** — implementation matches the task description; nothing out of scope added

The prompt must end with: *"Output exactly `APPROVED` or `CHANGES REQUESTED` followed by specific file+line feedback. Do not output general comments without a file and line number."*

### `AIRunner`
Three coder backends + three reviewer backends. Each has a primary attempt and one fallback. The `env_removals=["CLAUDECODE"]` trick is applied to all Claude calls.

```python
def assign_agents(self) -> tuple[str, str]   # (coder, reviewer)
def run_coder(self, agent, prompt) -> bool
def run_reviewer(self, agent, prompt) -> str
```

### `PreCommitGate`
Runs `uv run pre-commit run --all-files`. Detects failure by **exit code** (not grep). On failure, invokes coder with `PromptBuilder.precommit_fix_prompt()`. Retries up to `max_rounds`. If still failing after max rounds, logs a warning and continues anyway (CI is the backstop). Returns `PreCommitResult`.

### `TestRunner`
Reads `quality_checks` list from `prd.json` via `TaskTracker.get_quality_checks()`. Runs each command via `SubprocessRunner` — exit code determines pass/fail. On failure, invokes coder with `PromptBuilder.test_fix_prompt()` (similar to `PreCommitGate` pattern). Retries up to `max_test_fix_rounds`. After `max_test_fix_rounds` still failing: logs warning, continues (CI is the backstop). Returns `TestResult`.

### `TestWriter` (TDD mode only)

Active when `Config.tdd_mode = True`. Runs **before** the coder on a fresh branch, as a completely separate AI invocation — different model assignment, no shared context with the eventual coder.

```python
def write_tests(self, task: dict, branch: str) -> Path:
    """Invoke test-writer agent. Commits failing tests to the branch.
    Returns path to the test file written."""
    prompt = PromptBuilder.test_writer_prompt(task)
    self.ai_runner.run_test_writer(prompt)   # separate agent role in AIRunner
    # Test writer commits directly; orchestrator then validates before coder starts
```

The test writer is explicitly told: *"Write failing tests only. Do NOT implement the module under test. Tests must fail with ImportError or AssertionError — not pass."*

After the test writer commits, `TestQualityChecker` runs before the coder is invoked.

### `TestQualityChecker` (TDD mode only)

Two-tier validation of the test file written by `TestWriter`. Retries the test writer up to `max_test_write_rounds` if quality fails.

**Tier 1 — Deterministic (AST-based):**
```python
import ast

def _ast_checks(self, test_source: str, task: dict) -> list[str]:
    issues = []
    tree = ast.parse(test_source)
    test_fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name.startswith("test_")]

    if len(test_fns) < len(task["acceptance_criteria"]):
        issues.append(f"Fewer tests ({len(test_fns)}) than ACs ({len(task['acceptance_criteria'])})")

    for fn in test_fns:
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        if not asserts:
            issues.append(f"{fn.name}: no assertions")
            continue
        for a in asserts:
            # detect `assert True` and `assert 1 == 1` patterns
            if isinstance(a.test, ast.Constant) and a.test.value is True:
                issues.append(f"{fn.name}: trivially true assertion")
        body_stmts = [s for s in fn.body if not isinstance(s, (ast.Pass, ast.Expr))]
        if not body_stmts:
            issues.append(f"{fn.name}: empty or pass-only body")

    # check module under test is actually imported
    imports = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not any(task.get("title", "").split("_")[0] in imp for imp in imports):
        issues.append("Test file does not appear to import the module under test")
    return issues
```

**Tier 2 — AI semantic check** (runs only when Tier 1 passes):

Sends `PromptBuilder.test_quality_prompt(task, test_source, ast_report)` to an AI. The AI receives: the task ACs, the full test file, and the AST report (issue-free at this point). It is asked: *"Does each test genuinely verify its corresponding AC? Flag any test that checks implementation details instead of observable behaviour, or that would pass against a trivially wrong implementation."*

Output parsed for `[HOLLOW] <test_name>: <reason>` lines. Each flagged test is a `TestQualityResult.ai_issues` entry.

### `ReviewLoop`
Runs reviewer on PR diff. Parses verdict:
```python
# CHANGES REQUESTED takes precedence over APPROVED if both appear
if re.search(r'CHANGES\s+REQUESTED', text, re.IGNORECASE):
    return "CHANGES_REQUESTED"
return "APPROVED"  # if unclear, treat as approved (log it)
```
On `CHANGES_REQUESTED`: invokes coder with review text as fix prompt, pushes, re-reviews. After `max_rounds`: logs warning, continues to CI/merge (does not abort).

Before accepting any verdict, runs `ReviewQualityChecker`.

### `ReviewQualityChecker`

Validates every review before the `ReviewLoop` acts on it. Two tiers:

**Tier 1 — Deterministic:**
```python
def check(self, review_text: str, previous_reviews: list[str]) -> ReviewQualityResult:
    if len(review_text.split()) < 80:
        return ReviewQualityResult(False, "review too short (< 80 words)")
    if not re.search(r'(APPROVE|REQUEST\s+CHANGES)', review_text, re.IGNORECASE):
        return ReviewQualityResult(False, "no verdict found")
    if not re.search(r'\w+\.py:\d+|\w+/\w+\.\w+', review_text):
        return ReviewQualityResult(False, "no file/line references found")
    if previous_reviews and review_text.strip() == previous_reviews[-1].strip():
        return ReviewQualityResult(False, "identical to previous review (rubber-stamping)")
    return ReviewQualityResult(True, "ok")
```

**Tier 2 — AI meta-review** (runs only when Tier 1 passes, and only if `--deep-review-check` flag set or reviewer has failed quality twice this session):

Sends the review + the task ACs to an AI: *"Does this review address all the acceptance criteria? Does it cite specific code? Is the verdict justified by the issues found?"* Parsed for a PASS/FAIL.

**On quality failure**: retry with a different reviewer agent. After 2 failures from different agents: log `REVIEWER_QUALITY_FAILED`, fall back to `--skip-review` for this task, continue.

### `CIPoller`
Most complex component. Uses JSON CI output:
```python
checks = json.loads(gh_pr_checks_json)
pending = any(c["conclusion"] is None for c in checks)
failed  = any(c["conclusion"] in ("failure", "error") for c in checks)
# Empty checks list = still waiting (GitHub hasn't triggered CI yet)
```
On failure: fetches `gh run view --log-failed` (last 150 lines), invokes coder with `PromptBuilder.ci_fix_prompt()`, pushes fixes, re-polls. After `max_ci_fix_rounds` failures: raises `CIFailedFatal`.

### `DiscoveryWizard`
Used by `ralph init`. Asks exactly 6 questions interactively:

1. One-sentence product description
2. Language, runtime, package manager
3. Test framework + coverage tool
4. Quality gate commands (pre-commit, lint, test commands that will populate `quality_checks`)
5. Any human-only steps (credentials, infra provisioning — these become `owner: "human"` tasks)
6. What is explicitly out of scope for this sprint

Produces a `ProjectSpec` dataclass. Does **not** call AI — pure interactive I/O. Rationale: `/grill-me` is open-ended and relentless; `DiscoveryWizard` is tight and ralph-specific. 6 questions is enough to generate a valid `prd.json` scaffold without over-engineering.

### `PrdValidator`
Shared validation layer used by both `PrdGenerator` and `PlanChecker` (Tier 1). Enforces:

1. `description` ≥ 100 chars
2. At least one acceptance criterion references a file path (e.g. `tests/`, `.py`)
3. No credential strings in ralph-owned tasks (regex: `password|secret|token|key` in description)
4. All `depends_on` IDs exist somewhere in `prd.json`

Raises `PlanInvalidError` with specific field + reason on any failure. Kept separate from `PlanChecker` so `PrdGenerator` can validate before appending without instantiating the full plan checker.

### `PrdGenerator`
Used by `ralph add`. Accepts a natural language spec string or a GitHub issue URL. If a URL is passed, fetches issue body via `gh issue view`. Calls `AIRunner` with `PromptBuilder.prd_generate_prompt()` to produce task JSON, then runs `PrdValidator` before appending to `prd.json` via `TaskTracker.add_task()`. Infers the next epic prefix from the highest existing task ID. Mirrors the `/ralph-prd` skill logic but as a typed Python class.

### `PlanConsensus`
Used by `ralph plan`. Lightweight Planner + Critic loop (max 3 iterations), analogous to `/ralplan` but without an Architect agent:

1. **Planner** agent: produce a work plan from the `ProjectSpec`
2. **Critic** agent: review against quality gates — measurable ACs, atomic tasks, no vague language
3. If **REJECT**: send Critic feedback to Planner, increment iteration
4. If **OKAY** or max iterations: write plan to `ralph-plan.md` and return

Key difference from `/ralplan`: no Architect agent. For complex architectural decisions users can run `/ralplan` interactively first, then pass the result to `ralph plan`.

### `Orchestrator`
Main loop. Composes everything. `_preflight()` validates tools, auth, SSH remote, prd.json structure, then runs `PlanChecker.run()` — raising `PlanInvalidError` on structural errors, logging warnings if `--validate-plan` was passed. `_run_task()` is the per-iteration state machine (see below).

---

## Per-Task State Machine

**Standard mode:**
```
ensure_main_up_to_date()         → BranchSyncError → STOP
checkout_or_create(branch)       → exists+no-resume → STOP
                                 → exists+resume → skip coding if PR open
run_coder()                      → CoderFailedError → STOP
PreCommitGate.run()              → failure after max rounds → WARN, continue
TestRunner.run()                 → failure after max rounds → WARN, continue
push_branch()                    → CalledProcessError → STOP
PRManager.create() or get_existing()
ReviewLoop.run()                 → ReviewQualityChecker on each review
                                 → max rounds exceeded → WARN, continue
CIPoller.wait_and_fix()          → CIFailedFatal / CITimeoutError → STOP
PRDGuard.check()                 → PRDGuardViolation → close PR → STOP
PRManager.merge()
BranchManager.merge_and_cleanup()
TaskTracker.mark_complete()      }
TaskTracker.append_progress()    } orchestrator-owned, never delegated to AI
TaskTracker.commit_tracking()    }
→ ITERATION COMPLETE
```

**TDD mode** (`--tdd`): TestWriter and coder are **different agent invocations** — no shared context.
```
ensure_main_up_to_date()
checkout_or_create(branch)
TestWriter.write_tests()         → commits failing tests to branch
TestQualityChecker.check()       → hollow? → retry TestWriter → max rounds → STOP
run_coder()                      → coder sees pre-committed failing tests; makes them pass
PreCommitGate.run()
TestRunner.run()                 → must now pass (tests were green-lit by TestQualityChecker)
push_branch()
PRManager.create()
ReviewLoop.run()                 → ReviewQualityChecker on each review
CIPoller.wait_and_fix()
PRDGuard.check()
PRManager.merge()
… (same completion steps)
→ ITERATION COMPLETE
```

---

## CLI Interface

Replaces both `ralph-loop.sh` and `ralph-once.sh`:

```
rzilla [OPTIONS]          # installed via pipx
./ralph.py [OPTIONS]      # copied into project

--max N               Max iterations (default: 10)
--skip-review         Skip AI review, merge on CI pass
--tdd                 TDD mode: separate test-writer agent writes tests before coder
--claude-only         Claude for all agent roles
--gemini-only         Gemini for all agent roles
--opencode-only       opencode for all agent roles
--opencode-model STR  Override opencode model (default: opencode/kimi-k2.5)
--resume              Resume stale branches from interrupted runs
--max-test-fix-rounds N       Max AI fix rounds for test failures (default: 2)
--max-test-write-rounds N     TDD: max rounds to get hollow-free tests (default: 2)
--task TASK_ID        Force a specific task
--validate-plan       AI sanity check on prd.json before sprint (warns, does not block)
--no-decompose        Skip auto-decomposition of complexity-3 tasks
--deep-review-check   Enable AI meta-review quality check on every review
--dry-run             Print steps without executing AI calls or git ops
--repo-dir PATH       Repo root (default: directory containing ralph.py)
```

---

## File Layout in ralph.py

Top-to-bottom definition order:

1. Shebang + module docstring
2. Imports (stdlib → optional `rich` → optional `click`)
3. Constants
4. Exception classes
5. Dataclasses (`Config`, `PRInfo`, `CIResult`, `ReviewResult`, `PreCommitResult`, `BranchStatus`, `TaskResult`, `PlanCheckResult`, `TestQualityResult`, `ReviewQualityResult`)
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
17. `TestWriter`
18. `TestQualityChecker`
19. `ReviewLoop`
20. `ReviewQualityChecker`
21. `CIPoller`
22. `Orchestrator`
23. CLI entry point (click)
24. `main()`
20. `if __name__ == "__main__": sys.exit(main())`

---

## Implementation Phases

### Phase 1 — Skeleton
- Constants, exceptions, `Config`, `RalphLogger`, `SubprocessRunner`
- CLI argument parsing
- `main()` entry point

### Phase 2 — Data Layer
- `TaskTracker` — all prd.json/progress.txt access
- `PlanChecker` — structural validation + `--validate-plan` AI check
- Test both in isolation against real `prd.json`

### Phase 3 — Git/GitHub Layer
- `BranchManager`, `PRManager`, `PRDGuard`
- `verify_ssh_remote()` first — hard prerequisite

### Phase 4 — AI Layer
- `PromptBuilder` — port all prompts from bash version
- `AIRunner` — all 6 backend methods
- `PreCommitGate`
- skills management

### Phase 5 — Loop Logic
- `ReviewLoop`, `CIPoller`
- `Orchestrator` — `_preflight()`, `_check_stop_conditions()`, `_run_task()`

### Phase 6 — Integration
- `--dry-run` mode
- End-to-end test
- `chmod +x`, verify shebang

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Single file | No install, just `cp ralph.py`. Bash version was also one file. |
| Click for CLI | Already a project dependency (Playchitect uses it) |
| `TaskTracker` exclusively owns prd.json | Prevents AI from corrupting loop state |
| `PRDGuard` threshold 0 (not 1) | Coder is told not to touch prd.json at all — any change is a violation |
| JSON CI parsing via `conclusion` field | `state` can be `COMPLETED` even when `conclusion` is `failure` — use the right field |
| No `shell=True` anywhere | Eliminates quoting bugs and shell injection risk |
| `env_removals=["CLAUDECODE"]` | Claude calls fail inside a Claude Code session without this |

---

## Known Gotchas

- **`gh pr checks` on fresh PR** — returns empty list for 30-60s after push; treat `[]` as PENDING
- **`git pull --ff-only` failure** — never silently ignore; abort with clear message (diverged main causes push failures later)
- **`conclusion` vs `state`** — always use `conclusion` field from `gh pr checks --json`
- **Branch name sanitisation** — sanitise task title for filesystem safety: `re.sub(r'[^a-z0-9-]', '-', title.lower())[:40]`
- **Prompt as list arg** — `subprocess.run([cli, flags, prompt_text])` avoids all shell quoting and `ARG_MAX` issues
- **Required vs optional CI checks** — currently aborts on any failure; improvement would be to only block on required checks via branch protection API
- **CI stale-data race after fix push** — after pushing a CI fix, GitHub takes 10-30s to register the new run. Polling `gh pr checks` immediately reads the still-present FAILED state from the previous run and misclassifies it as a new failure. Two-part fix: (1) capture the current run ID before pushing, then wait until `gh run list` returns a different ID before polling; (2) poll the pinned run directly via `gh run view {run_id}` rather than `gh pr checks`, making it immune to stale data from other runs on the same branch.
- **`pull --ff-only` kills the loop silently after every merge** — after `gh pr merge`, remote main has the new merge commit but local main may have diverged (e.g. a previous tracking commit that didn't push cleanly). `set -euo pipefail` kills the script with no log message. Always use `git fetch origin main && git reset --hard origin/main` — unconditional, never fails due to divergence.

---

## Contributions from Production Experience (piewise / etf-lens)

### 1. `BranchManager.ensure_main_up_to_date()` — use `reset --hard`, not `pull --ff-only`

Skip `pull --ff-only` entirely. Use `fetch + reset --hard`:

```python
def ensure_main_up_to_date(self):
    self.run(["git", "checkout", MAIN_BRANCH])
    self.run(["git", "fetch", "origin", MAIN_BRANCH])
    self.run(["git", "reset", "--hard", f"origin/{MAIN_BRANCH}"])
```

`pull --ff-only` fails if the local main has a stale commit (e.g. a previous `mark_complete` push that diverged). `reset --hard` is unconditional. This is the direct fix for sequential-task merge conflicts on `progress.txt`.

### 2. `TaskTracker` — array order, not `priority` sort

`get_next_task()` must iterate the `tasks` array in order — do not sort by `priority`. Priority is human-readable documentation. Sorting by it caused multiple "loop stops at human task" bugs where human tasks with a lower priority number blocked ralph tasks.

```python
def get_next_task(self) -> dict | None:
    for task in self.load()["tasks"]:  # array order only
        if not task.get("completed") and not task.get("blocked") and task.get("owner") == "ralph":
            return task
    return None
```

### 3. `PRDGuard` — check working tree after coder, before push

Add a cheap working-tree check immediately after the coder runs, before the AI fix loop and push:

```
run_coder()
→ PRDGuard.check_working_tree()   # git diff --name-only HEAD -- prd.json progress.txt
→ PreCommitGate.run()
→ TestRunner.run()
→ push_branch()
→ PRDGuard.check_pr_diff()        # existing pre-merge check
```

### 4. `TaskTracker.mark_complete()` — guard against double-marking

```python
def mark_complete(self, task_id: str) -> None:
    prd = self.load()
    task = next((t for t in prd["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise PRDGuardViolation(f"task {task_id} not found")
    if task.get("completed"):
        raise PRDGuardViolation(f"task {task_id} already marked complete — possible bulk-marking attack")
    task["completed"] = True
    # write back
```

### 5. `PreCommitGate` — auto-fix before invoking AI

Run `ruff --fix` before the AI fix loop. Most pre-commit failures are auto-fixable and don't need an AI roundtrip:

```python
self.runner.run(["uv", "run", "ruff", "check", "--fix", "."])
self.runner.run(["uv", "run", "ruff", "format", "."])
result = self._run_pre_commit()
if result.returncode == 0:
    return PreCommitResult(passed=True, rounds_used=0)
# only now invoke AI fix loop
```

### 6. `CIPoller` — pin to run ID, not PR checks

**Lesson from playchitect production run (2026-03-15):** polling `gh pr checks` after a fix push reads stale data from the previous failed run, causing the loop to treat an already-fixed failure as a new one.

The correct pattern:

```python
def wait_and_fix(self, pr_number, branch, task, coder):
    for _round in range(self.max_fix_rounds + 1):
        # Pin to the specific run triggered by this push
        run_id = self._get_latest_run_id(branch)
        self._wait_for_run(run_id)   # polls gh run view {run_id} directly
        ...
        if failed and _round < self.max_fix_rounds:
            prev_run_id = run_id
            # push fix, then wait for NEW run to register
            self._push_fix(...)
            run_id = self._wait_for_new_run(branch, prev_run_id)  # polls until ID changes

def _wait_for_new_run(self, branch: str, prev_run_id: str, timeout: int = 180) -> str:
    # Polls gh run list --branch every 10s until a different run ID appears.
    # Raises CITimeoutError if no new run after timeout seconds.

def _wait_for_run(self, run_id: str) -> str:
    # Polls gh run view {run_id} --json status,conclusion every 30s.
    # Returns "PASSED" or "FAILED". Never reads from gh pr checks.
```

Also pin the field list when falling back to `gh pr checks`:
```python
["gh", "pr", "checks", str(pr_number), "--json", "name,state,conclusion,required"]
```
The `required` field enables filtering to only block on required checks (optional checks should warn, not abort).

### 7. `PromptBuilder.coder_prompt(resume=True)` — tell the coder to inspect what's there

```
IMPORTANT: This branch already has commits. Run `git log --oneline` and
`git diff origin/main...HEAD` to see what is already implemented.
Do NOT re-implement work that is already committed.
```

---

## Contributions from Production Experience (playchitect, 2026-04-20)

### 8. `AIRunner` — nested Claude session detection

**Lesson:** `env_removals=["CLAUDECODE"]` is not sufficient. When `claude --print` is called as a subprocess from inside a Claude Code session, it exits 0 but produces no review output and posts nothing. The root cause is that the Claude CLI detects it is being invoked inside an existing Claude Code process and silently no-ops, regardless of whether `CLAUDECODE` is unset.

`AIRunner` must detect this at startup and skip the `claude` reviewer path entirely:

```python
def _is_nested_claude_session(self) -> bool:
    """True when ralph is running inside a Claude Code session.
    Calling `claude --print` in this environment exits 0 but produces no output.
    """
    return "CLAUDECODE" in os.environ

def run_reviewer(self, agent: str, prompt: str) -> str:
    if agent == "claude" and self._is_nested_claude_session():
        self.log.warn(
            "Nested Claude session detected — claude reviewer unavailable. "
            "Falling back to gemini."
        )
        return self.run_reviewer("gemini", prompt)
    ...
```

`Orchestrator._preflight()` should also log a prominent warning if `CLAUDECODE` is set, so the operator knows review fallback is active.

The Scrum Master should default to `--skip-review` when it detects it is itself running inside Claude Code, and note this in the session log.

---

### 9. `Orchestrator._preflight()` — assert prd.json is clean before starting

**Lesson:** The Scrum Master writes new tasks to `prd.json` then calls `ralph.py`. But ralph's first action is `ensure_main_up_to_date()` which does `git reset --hard origin/main` — silently wiping any uncommitted Scrum Master edits. In the playchitect run this caused GUI-07 through GUI-12 to be lost and required manual re-entry.

Two-part fix:

1. **`BacklogManager.create_task()`** must commit and push before returning. Never leave prd.json dirty on disk.

2. **`Orchestrator._preflight()`** must assert prd.json matches origin:

```python
def _preflight(self):
    ...
    result = self.runner.run(["git", "diff", "--quiet", "origin/main", "--", PRD_FILE])
    if result.returncode != 0:
        raise PreflightError(
            "prd.json has uncommitted local changes that will be wiped by "
            "ensure_main_up_to_date(). Commit and push prd.json before running ralph."
        )
```

---

### 10. `UnblockStrategy` — auto-downgrade to `--skip-review` on repeated reviewer failure

**Lesson:** When the reviewer consistently fails (kimi timeout + nested-Claude fallback both failing), the loop dies and requires manual restart with `--skip-review`. The Scrum Master should handle this automatically.

Add `REVIEWER_UNAVAILABLE` to `BlockerKind` and a matching unblock strategy:

```python
class BlockerKind(Enum):
    ...
    REVIEWER_UNAVAILABLE = "reviewer_unavailable"   # reviewer timed out / no output N times in a row
```

```python
# In UnblockStrategy
if blocker == BlockerKind.REVIEWER_UNAVAILABLE:
    consecutive = sum(1 for b in self.circuit_breaker.recent_blockers
                      if b == BlockerKind.REVIEWER_UNAVAILABLE)
    if consecutive >= 2:
        self.log.warn("Reviewer failing repeatedly — switching to --skip-review for session")
        self.config.skip_review = True
        self._notify("Reviewer unavailable; continuing with CI-only gate.")
        return True   # unblocked — restart ralph with updated config
```

CI + pre-commit are a sufficient quality gate for well-specified tasks. Don't let a flaky reviewer kill a sprint.

---

### 11. `ScrumMaster` — branch cleanup as a post-sprint responsibility

**Lesson:** After a multi-sprint project, 40+ stale local and remote ralph branches accumulate. The plan has no cleanup step. Add to the `NO_TASKS_REMAINING` handler:

```python
def _post_sprint_cleanup(self):
    """Delete local and remote branches for all completed ralph tasks."""
    prd = self.task_tracker.load()
    completed_branches = [
        f"ralph/task-{t['id']}-{self._sanitise(t['title'])}"
        for t in prd["tasks"]
        if t.get("completed") and t.get("owner") != "human"
    ]
    for branch in completed_branches:
        self.branch_manager.delete_local(branch, ignore_missing=True)
        self.branch_manager.delete_remote(branch, ignore_missing=True)
    # Also prune stale remote-tracking refs
    self.runner.run(["git", "fetch", "--prune", "origin"])
    self.log.info(f"Cleaned up {len(completed_branches)} stale branches.")
```

Expose as `--no-cleanup` flag if the operator wants to inspect branches post-sprint.

---

### 12. `LoopSupervisor` — verify clean exit by log content, not exit code

**Lesson:** The loop exited with code 0 twice when the reviewer step silently failed mid-iteration. Exit code 0 is not a reliable signal of success — it only means no uncaught exception propagated to the shell.

`LoopSupervisor` must cross-check the log after every ralph run:

```python
CLEAN_EXIT_MARKERS = [
    "Loop finished.",
    "ALL RALPH TASKS COMPLETE",
    "YOUR TURN",          # human task next — expected stop
    "HUMAN_TASK_NEXT",
]

def _loop_exited_cleanly(self, exit_code: int, log_path: Path) -> bool:
    if exit_code != 0:
        return False
    last_lines = log_path.read_text().splitlines()[-30:]
    if not any(any(marker in line for marker in CLEAN_EXIT_MARKERS) for line in last_lines):
        self.log.warn(
            "ralph exited 0 but no clean-exit marker found in log — "
            "treating as UNKNOWN_ERROR"
        )
        return False
    return True
```

Without this, a silently-failed reviewer causes the Scrum Master to think the sprint completed when it only completed one task.

---

### 13. `PromptBuilder` — per-epic tech-stack addendum registry

**Lesson:** A GUI task shipped a runtime API error (`Gtk.Display.get_default()` → crash) that passed all tests and pre-commit hooks because static analysis cannot detect framework runtime APIs. The reviewer prompt is the last line of defence — but only if it knows what to look for.

The root issue is framework-specific: GTK4, React, SwiftUI, WinUI, Electron and others each have runtime gotchas that linters won't catch (removed APIs, threading requirements, SSR vs client-only calls, wrong import namespaces). These are **project-specific knowledge**, not things Ralphzilla should hardcode.

**Design:** `PromptBuilder` reads an optional `epic_addenda` map from `prd.json`. For each task, it looks up the task's `epic` field in that map and appends the matching text to the Correctness category of the reviewer prompt.

```python
# prd.json — project configures its own addenda
{
  "epic_addenda": {
    "GUI": "Check for framework runtime API errors: wrong namespace, removed API, missing version declaration, threading violations. Flag as blocking Correctness issue.",
    "API": "Verify all external HTTP calls have timeout set. No bare except around network errors.",
    "DB":  "All queries must be parameterised — no string interpolation into SQL."
  },
  "tasks": [...]
}

# PromptBuilder — framework-agnostic
@staticmethod
def reviewer_prompt(task: dict, diff: str, prd: dict, ...) -> str:
    base = ...  # existing 6-category prompt
    addendum = prd.get("epic_addenda", {}).get(task.get("epic", ""), "")
    if addendum:
        base += f"\n\n**Epic-specific checks ({task['epic']}):**\n{addendum}"
    return base
```

**Examples of project-level addenda:**
| Stack | Example addendum |
|---|---|
| GTK4 / libadwaita | Use `Gdk.Display`, not `Gtk.Display`; `gi.require_version("Gdk", "4.0")` required |
| React / Next.js | No hooks in conditionals; no `window`/`document` in SSR paths |
| SwiftUI | `@MainActor` on UI updates; `ObservableObject` needs `@Published` |
| WinUI 3 | UI calls must be on the dispatcher thread |
| Electron | No `require()` in renderer without `nodeIntegration`; use `contextBridge` |

The key principle: **Ralphzilla provides the mechanism; the project provides the knowledge.**

---

## Scrum Master (AI Orchestrator)

The Scrum Master is a layer above `ralph.py`. Where ralph is a sprint executor, the Scrum Master is the sprint owner: it runs ralph, watches for failures, unblocks them, manages the backlog, and never lets a recoverable failure kill the sprint.

### Responsibilities

1. **Loop supervision** — start ralph, detect exit cause, decide whether to resume or intervene
2. **Unblocking** — diagnose why ralph stopped and attempt a fix before re-running
3. **Backlog hygiene** — observe PR reviews and create follow-up FIX tickets for minor issues noted in an APPROVED review
4. **Escalation** — if it cannot unblock after N attempts, pause and ask the human

### Architecture

```
ScrumMaster
├── LoopSupervisor      — runs ralph.py as subprocess, watches exit code + log
├── BlockerAnalyser     — classifies why ralph stopped (see below)
├── UnblockStrategy     — per-blocker fix strategies
├── BacklogManager      — creates FIX tickets; never modifies in-flight tasks
└── EscalationManager   — circuit breaker; escalates to human when stuck
```

### Blocker Classification

```python
class BlockerKind(Enum):
    MERGE_CONFLICT     = "merge_conflict"      # git merge failed
    CI_FAILED_FATAL    = "ci_failed_fatal"     # CI exhausted fix rounds
    PRD_GUARD          = "prd_guard"           # coder touched prd.json
    PRECOMMIT_FATAL    = "precommit_fatal"     # pre-commit exhausted fix rounds
    REVIEWER_REJECT    = "reviewer_reject"     # review max rounds exceeded
    HUMAN_TASK_NEXT    = "human_task_next"     # next task is owner:human
    NO_TASKS_REMAINING = "no_tasks_remaining"  # all done
    UNKNOWN_ERROR      = "unknown_error"       # unclassified
```

### Unblock Strategies

| Blocker | Strategy |
|---|---|
| `MERGE_CONFLICT` | Auto-resolve (same approach as described in this doc). Re-run ralph. |
| `CI_FAILED_FATAL` | Analyse CI logs, create a FIX task, re-run ralph on the fix task |
| `PRD_GUARD` | Close the bad PR, revert any prd.json corruption, re-run the same task |
| `PRECOMMIT_FATAL` | Create a FIX task targeting the specific file/lint issue; re-run |
| `HUMAN_TASK_NEXT` | Notify human, pause loop (do not attempt to unblock autonomously) |
| `NO_TASKS_REMAINING` | Sprint complete — generate summary, notify human |
| `UNKNOWN_ERROR` | Log full context, escalate to human immediately |

### Circuit Breakers — Preventing Infinite Loops

This is critical. The Scrum Master must not itself become an infinite loop or a ping-pong machine.

```python
@dataclass
class CircuitBreaker:
    max_unblock_attempts_per_task: int = 3    # same task fails 3 times → escalate
    max_total_restarts: int = 10              # total ralph restarts per session
    min_cooldown_secs: int = 30               # minimum wait between restarts
    max_fix_tasks_per_session: int = 5        # FIX tickets created this session
    pingpong_window: int = 3                  # same blocker N times in a row → escalate
```

State tracked per session:

```python
restart_count: int = 0
per_task_attempts: dict[str, int] = {}    # task_id → attempt count
recent_blockers: deque[BlockerKind]       # last N blockers (pingpong detection)
fix_tasks_created: int = 0
```

Escalation triggers:
- Same `task_id` has failed `max_unblock_attempts_per_task` times → mark task `blocked`, notify human
- `recent_blockers` last N entries are all the same kind → ping-pong detected → escalate
- `restart_count >= max_total_restarts` → session limit hit → escalate
- `fix_tasks_created >= max_fix_tasks_per_session` → backlog growing faster than it shrinks → escalate

### Backlog Management — Creating FIX Tickets from Approved Reviews

When a PR is APPROVED but the review contains minor comments (style suggestions, edge case notes, etc.), the Scrum Master should capture these as FIX tasks rather than letting them disappear.

**Safety constraints** — this is where conflicts can arise:

1. **Never create a FIX task for a file that has an in-flight branch touching it.** Check `git branch -r` for active ralph branches and diff them against the affected files.
2. **FIX task is created on main after the PR merges** — never while the branch is open.
3. **One FIX task per PR review** — do not fragment a single review into multiple tickets.
4. **Only create if the review comment mentions a specific file/function** — vague style notes ("consider renaming this") do not become tickets.
5. **Mark the FIX task with `note: "from PR #N review"` and low priority** — it should not jump the queue ahead of planned work.

```python
def maybe_create_review_fix_task(self, pr_number: int, review_text: str, verdict: str):
    if verdict != "APPROVED":
        return  # CHANGES_REQUESTED tasks are already handled by ReviewLoop
    actionable = self._extract_actionable_comments(review_text)
    if not actionable:
        return
    in_flight_files = self._get_in_flight_touched_files()
    safe_comments = [c for c in actionable if c.file not in in_flight_files]
    if safe_comments:
        self.backlog_manager.create_fix_task(safe_comments, source_pr=pr_number)
```

### Scrum Master CLI

```
./scrum-master.py [OPTIONS]

--project-dir PATH    Project root containing ralph.py and prd.json
--max-restarts N      Max times to restart ralph (default: 10)
--notify EMAIL/SLACK  Where to send escalation alerts
--dry-run             Simulate without executing
--session-log PATH    Where to write session state (for resume after crash)
```

### Session State Persistence

The Scrum Master writes its own state file (`scrum-session.json`) so it can be resumed after a crash:

```json
{
  "session_id": "2026-03-15-001",
  "restart_count": 2,
  "per_task_attempts": {"TASK-05": 2},
  "fix_tasks_created": 1,
  "last_blocker": "ci_failed_fatal",
  "status": "running"
}
```

---

## Planner (BA Skill)

The Planner is the front-end of the whole system — the step before ralph ever runs. Its job is to act like a Business Analyst: interview the human, translate intent into requirements, and produce a PRD that ralph can execute without ambiguity.

The existing `ralph-prd` Claude Code skill handles the *writing* of tasks. The Planner is a separate, deeper process that handles the *discovery* before writing.

### Where it lives

A Claude Code skill: `/home/james/.claude/skills/ralph-planner/SKILL.md`

Invoked when the human says "I want to build X" or "plan this project" — before any prd.json exists.

### Planner Phases

**Phase 1 — Discovery Interview**

The Planner asks structured questions to extract what the human actually needs. It does not accept vague answers — it probes until it has enough to write unambiguous acceptance criteria.

Questions it must get answered:
- What does the finished product do in one sentence?
- Who uses it and how? (CLI, API, web UI, cron job)
- What are the hard constraints? (language, dependencies, deployment target)
- What does "done" look like for the first version? (MVP scope)
- What explicitly is OUT of scope?
- What external systems does it touch? (APIs, databases, auth providers)
- Are there any human-only steps? (credentials, provisioning, approvals)
- What are the quality gates? (tests, linting, coverage threshold)

**Phase 2 — Scope Negotiation**

Before writing any tasks, the Planner presents a one-paragraph project summary and a bullet list of what it plans to build. The human must confirm or cut scope.

Anti-creep gate: the Planner flags any feature that:
- requires more than one external API
- requires a UI (web or otherwise) unless explicitly requested
- requires auth/credentials not already available
- would take more than ~5 ralph tasks to implement

These get flagged as "Phase 2 candidates" — deferred, not included in the MVP prd.json.

**Phase 3 — Task Generation**

Only after scope is confirmed does the Planner invoke the `ralph-prd` skill logic to generate tasks. It outputs:
- Complete `prd.json` with all tasks
- A brief "sprint brief" in plain English: what ralph will build, in what order, and what the human needs to do first (TASK-H tasks)

**Phase 4 — PRD Review**

The Planner presents the prd.json to the human and asks:
- "Does the order look right?"
- "Are any acceptance criteria too vague or too strict?"
- "Are there tasks here you want to defer?"

Only after explicit human sign-off does the Planner write `prd.json` to disk.

### Planner Output Contract

The prd.json produced by the Planner must pass a validation check before being written:

```python
def validate_prd(prd: dict) -> list[str]:
    errors = []
    for task in prd["tasks"]:
        if len(task["description"]) < 100:
            errors.append(f"{task['id']}: description too short (AI will under-implement)")
        if "tests/" not in task["acceptance_criteria"]:
            errors.append(f"{task['id']}: acceptance criteria must reference a test file")
        if task["owner"] == "ralph" and any(
            word in task["description"].lower()
            for word in ["secret", "api key", "password", "credential"]
        ):
            errors.append(f"{task['id']}: ralph task references credentials — should be owner:human")
    return errors
```

### Relationship between Planner, Scrum Master, and ralph

```
Human intent
     │
     ▼
 Planner (BA)          ← interviews human, produces prd.json
     │  (human sign-off)
     ▼
Scrum Master           ← owns the sprint, supervises the loop
     │
     ▼
  ralph.py             ← executes tasks, opens PRs, merges
     │
     ▼
  Merged code
```

The human is only involved at two points: Planner sign-off, and when the Scrum Master escalates a blocker it cannot resolve.

---

## Future Consideration: LangGraph for Parallel Execution

**Do not build this now.** The sequential Scrum Master described above (plain Python state machine) is the right starting point. Revisit LangGraph if and when the project meets the criteria below.

### When LangGraph becomes worth it

LangGraph earns its dependency weight when the Scrum Master needs to **fan out to multiple concurrent worker nodes** — i.e. running three ralph instances on three independent tasks simultaneously using `git worktree`. At that point the concurrency coordination (barriers, shared state, fan-in ordering) is exactly the boilerplate LangGraph eliminates. Before that point it's overhead.

**Trigger criteria — consider LangGraph when ALL of the following are true:**

1. Projects regularly have 30+ tasks
2. Tasks decompose into clearly independent epics (Storage, CLI, API) with no cross-epic file dependencies
3. Sequential ralph is a meaningful bottleneck (sprints taking >1 day for a single project)
4. The prd.json schema has been extended with `depends_on` and `parallelisable` fields (see below)

If you're at 16-task projects running in an hour or two, the complexity cost of LangGraph outweighs the gain.

### What the parallel architecture would look like

```
ScrumMaster
├── PRDAnalyser        — builds DAG from depends_on, identifies parallelisable batches
├── WorkerPool         — fans out to N ralph.py instances in N git worktrees
│   ├── WorkerNode(A)  — own worktree, own branch
│   ├── WorkerNode(B)
│   └── WorkerNode(C)
├── MergeQueue         — serialised fan-in; merges PRs in dependency order,
│                        re-tests each branch against updated main before merging
├── BacklogManager     — creates FIX tasks after merges (unchanged)
└── CircuitBreaker     — per-worker limits + global session limit
```

### Concurrency strategy — threading vs multiprocessing

Each `WorkerNode` runs a `ralph.py` subprocess in an isolated git worktree. The workers themselves are light coordination wrappers — the heavy work happens inside the subprocess. Two options for managing those wrappers concurrently:

**`multiprocessing` (default, always safe)**
- True OS-level parallelism regardless of Python build
- Higher spawn overhead (~100–300ms per worker)
- No shared memory between workers (clean isolation)
- Works with any C extension, no GIL concerns

**`threading` (preferred if free-threaded Python 3.13t+ is available)**
- True parallelism without process spawn overhead
- Workers share the same process memory — lower overhead, faster fan-in
- Requires GIL-disabled Python build *and* all C extensions must be free-threaded safe
- GIL status is detectable at runtime:

```python
import sys

def _is_free_threaded() -> bool:
    """True if running under a GIL-disabled Python build (3.13t+)."""
    return getattr(sys, "_is_gil_enabled", lambda: True)() is False

def make_worker_pool(n: int) -> concurrent.futures.Executor:
    if _is_free_threaded():
        logger.info("Free-threaded Python detected — using ThreadPoolExecutor")
        return concurrent.futures.ThreadPoolExecutor(max_workers=n)
    logger.info("Standard Python — using ProcessPoolExecutor")
    return concurrent.futures.ProcessPoolExecutor(max_workers=n)
```

Since the workers are primarily coordinating subprocesses (not doing heavy Python computation), the GIL is rarely held anyway — `multiprocessing` overhead dominates over GIL contention in practice. Free-threaded mode is a nice optimisation, not a requirement.

Each worker runs in an isolated git worktree:

```bash
git worktree add ../project-task-05 -b ralph/task-05
git worktree add ../project-task-06 -b ralph/task-06
git worktree add ../project-task-07 -b ralph/task-07
```

### prd.json schema additions needed first

Before parallel execution is possible, the prd.json schema needs two new fields:

```json
{
  "id": "TASK-07",
  "depends_on": ["TASK-06"],
  "parallelisable": true,
  ...
}
```

- `depends_on`: list of task IDs that must be merged before this task can start
- `parallelisable`: false for tasks that touch shared infrastructure files (pyproject.toml, cli.py entrypoint, db schema) where concurrent edits are likely to conflict

**Add these fields to the schema now** even though parallel execution isn't being built yet. They're cheap to add, the Planner can populate them during discovery, and having them in the data means the parallel Scrum Master can be introduced later without a schema migration.

### New failure modes parallel execution introduces

| Problem | Mitigation |
|---|---|
| Task B depends on code Task A is creating | `depends_on` DAG — Scrum Master won't start B until A is merged |
| Two workers edit the same file | `parallelisable: false` on tasks that touch shared files |
| PR merge order matters (second merge conflicts with first) | MergeQueue merges sequentially, re-bases each subsequent branch against updated main |
| `prd.json` write contention | Fan-in node owns all `mark_complete` writes; worker nodes never touch prd.json |
| One worker failing shouldn't kill others | Per-worker circuit breakers; failed worker is parked, others continue |

---

## Source Files for Implementation

- `ralph-loop.sh` — current behaviour to replicate (prompts, fallback chains, polling logic)
- `ralph-once.sh` — single-task variant to merge into `--task` flag
- `prd.json` — definitive task schema
- `pyproject.toml` — confirms `click` is available, Python 3.13+ target
- `CLAUDE.md` — branch naming, commit style, no AI attribution in git messages
