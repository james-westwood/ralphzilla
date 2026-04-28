#!/usr/bin/env python3
"""
ralph_mcp.py — FastMCP server for ralphzilla AI sprint runner.

Monitoring tools (read-only):
- rzilla_status: Get sprint status overview
- rzilla_tasks: List tasks with filtering
- rzilla_log: Get last N lines of progress log
- rzilla_summary: Get latest sprint summary

Execution tools (granular, one step at a time — for scrum master control):
- rzilla_next_task: Get the next task to work on
- rzilla_start_task: Create branch and prepare for coding
- rzilla_run_coder: Invoke AI coder on current branch
- rzilla_run_precommit: Run pre-commit checks (lint/format)
- rzilla_run_tests: Run pytest and fix failures
- rzilla_push_branch: Push branch and create PR
- rzilla_run_review: Invoke AI reviewer on the PR
- rzilla_wait_ci: Wait for CI and return result
- rzilla_merge_task: Merge PR, mark task complete
- rzilla_commit_partial: Rescue uncommitted work from failed coder

Legacy tools (fire-and-forget, still available):
- rzilla_run: Start a full sprint as background process
- rzilla_dry_run: Run a dry-run simulation
- rzilla_add: Add a new task to the backlog
- rzilla_abort: Abort running sprint

Usage:
    # From any repo (auto-detects git root from cwd):
    uv run --extra mcp python ralph_mcp.py
    # For MCP client config (.mcp.json / opencode.json), use absolute venv path:
    # /path/to/ralphzilla/.venv/bin/python /path/to/ralphzilla/ralph_mcp.py
    # To target a specific project's prd.json:
    #   ralph_mcp.py --repo-dir /path/to/project

By default, the server resolves the project directory by walking up from cwd
to find the nearest .git directory. This means each project's .mcp.json does
not need to specify --repo-dir — just set cwd to the project root.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("MCP_LOG_LEVEL", "ERROR")

import psutil
from mcp.server.fastmcp import FastMCP

import ralph
from ralph import (
    AIRunner,
    BranchManager,
    Config,
    PreCommitGate,
    PRManager,
    RalphLogger,
    SubprocessRunner,
    TaskTracker,
    TestRunner,
)

RALPH_DIR = Path(__file__).parent


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from start to find the git repo root.

    Searches for a .git directory starting at start and walking up.
    Falls back to start if no git root is found.
    """
    candidate = (start or Path.cwd()).resolve()
    while True:
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return (start or Path.cwd()).resolve()


REPO_DIR = _find_repo_root()


def _set_project_dir(project_dir: Path) -> None:
    global PROJECT_DIR, PRD_FILE, PROGRESS_FILE, LOG_FILE, _repo_dir_flag

    PROJECT_DIR = project_dir
    PRD_FILE = PROJECT_DIR / "prd.json"
    PROGRESS_FILE = PROJECT_DIR / "progress.txt"
    LOG_FILE = PROJECT_DIR / "ralph-loop.log"
    _repo_dir_flag = ["--repo-dir", str(PROJECT_DIR)]


def _parse_repo_dir_args(argv: list[str]) -> tuple[Path, list[str]]:
    project_dir = _find_repo_root()
    cleaned_argv = [argv[0]]
    i = 1

    while i < len(argv):
        arg = argv[i]
        if arg == "--repo-dir":
            if i + 1 >= len(argv):
                print("Error: --repo-dir requires a path argument", file=sys.stderr)
                sys.exit(1)
            project_dir = Path(argv[i + 1]).expanduser().resolve()
            if not project_dir.is_dir():
                print(f"Error: --repo-dir {project_dir} is not a directory", file=sys.stderr)
                sys.exit(1)
            i += 2
            continue
        cleaned_argv.append(arg)
        i += 1

    return project_dir, cleaned_argv


_set_project_dir(REPO_DIR)

if __name__ == "__main__":
    _project_dir_arg, _cleaned_argv = _parse_repo_dir_args(sys.argv)
    _set_project_dir(_project_dir_arg)
    sys.argv[:] = _cleaned_argv
mcp = FastMCP("rzilla")


def _configure_mcp_logging() -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if (
            handler.__class__.__module__ == "rich.logging"
            and handler.__class__.__name__ == "RichHandler"
        ):
            root_logger.removeHandler(handler)

    for logger_name in ("mcp", "mcp.server.fastmcp"):
        logger = logging.getLogger(logger_name)
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.ERROR)
        logger.propagate = False


_configure_mcp_logging()


# --- Helper Functions ---


def _read_prd() -> dict:
    """Read and parse prd.json from disk."""
    if not PRD_FILE.exists():
        return {"tasks": []}
    with open(PRD_FILE, encoding="utf-8") as f:
        return json.load(f)


def _find_latest_summary() -> Path | None:
    """Find the most recent ralph-summary-*.md file."""
    summaries = sorted(PROJECT_DIR.glob("ralph-summary-*.md"))
    return summaries[-1] if summaries else None


def _make_config(**overrides) -> Config:
    """Build a Config pointing at the current PROJECT_DIR."""
    defaults = dict(
        max_iterations=1,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model=ralph.DEFAULT_OPENCODE_MODEL,
        opencode_reviewer_model=ralph.DEFAULT_OPENCODE_REVIEWER_MODEL,
        opencode_test_writer_model=ralph.DEFAULT_OPENCODE_TEST_WRITER_MODEL,
        resume=False,
        repo_dir=PROJECT_DIR,
        log_file=LOG_FILE,
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_logger() -> RalphLogger:
    return RalphLogger(LOG_FILE)


def _make_runner(logger: RalphLogger | None = None) -> SubprocessRunner:
    return SubprocessRunner(logger or _make_logger())


def _make_task_tracker(logger: RalphLogger | None = None) -> TaskTracker:
    return TaskTracker(
        PRD_FILE,
        PROGRESS_FILE,
        _make_runner(logger),
        logger or _make_logger(),
    )


def _is_sprint_running() -> bool:
    """Check if there's a rzilla run process active using psutil."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if cmdline and "rzilla" in " ".join(cmdline) and "run" in cmdline:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _get_rzilla_pid() -> int | None:
    """Get the PID of the running rzilla process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if cmdline and "rzilla" in " ".join(cmdline) and "run" in cmdline:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


# --- MCP Tools ---


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_status() -> str:
    """Get ralphzilla sprint status overview.

    Returns JSON with:
    - pending_tasks: count of incomplete tasks
    - completed_tasks: count of completed tasks
    - total_tasks: total number of tasks
    - next_task: {id, title} or null if no pending tasks
    - sprint_running: bool indicating if rzilla is currently running
    - last_summary: filename of most recent ralph-summary-*.md or null
    """
    prd = _read_prd()
    tasks = prd.get("tasks", [])

    completed = sum(1 for t in tasks if t.get("completed", False))
    pending = len(tasks) - completed

    # Find next task (first incomplete ralph-owned task with deps satisfied)
    next_task = None
    completed_ids = {t["id"] for t in tasks if t.get("completed", False)}
    for t in tasks:
        if t.get("completed", False):
            continue
        if t.get("owner") == "human":
            continue
        deps = t.get("depends_on", [])
        if all(d in completed_ids for d in deps):
            next_task = {"id": t["id"], "title": t.get("title", "")}
            break

    summary_file = _find_latest_summary()

    result = {
        "pending_tasks": pending,
        "completed_tasks": completed,
        "total_tasks": len(tasks),
        "next_task": next_task,
        "sprint_running": _is_sprint_running(),
        "last_summary": summary_file.name if summary_file else None,
    }

    return json.dumps(result, indent=2)


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_tasks(filter: str = "all", limit: int = 50) -> str:
    """List tasks from prd.json with optional filtering.

    Args:
        filter: One of "all", "pending", "completed" (default: "all")
        limit: Maximum number of tasks to return (default: 50, max 100)

    Returns:
        JSON array of task objects with id, title, owner, completed, priority
    """
    prd = _read_prd()
    tasks = prd.get("tasks", [])

    # Apply filter
    if filter == "pending":
        tasks = [t for t in tasks if not t.get("completed", False)]
    elif filter == "completed":
        tasks = [t for t in tasks if t.get("completed", False)]

    # Apply limit
    limit = min(limit, 100)
    tasks = tasks[:limit]

    # Simplify task objects for output
    result = [
        {
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "owner": t.get("owner", "ralph"),
            "completed": t.get("completed", False),
            "priority": t.get("priority", 0),
        }
        for t in tasks
    ]

    return json.dumps(result, indent=2)


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_log(lines: int = 20) -> str:
    """Return last N lines of progress.txt.

    Args:
        lines: Number of lines to return (default: 20, max: 100)

    Returns:
        String content of the last N lines, or "No progress log found."
    """
    if not PROGRESS_FILE.exists():
        return "No progress log found."

    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            all_lines = f.readlines()

        lines = min(lines, 100)
        last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(last_lines)
    except OSError:
        return "No progress log found."


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_summary() -> str:
    """Return content of most recent ralph-summary-*.md file.

    Returns:
        Full markdown content of the summary file, or "No sprint summary found."
    """
    summary_file = _find_latest_summary()

    if not summary_file:
        return "No sprint summary found."

    try:
        with open(summary_file, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "No sprint summary found."


# --- Granular Execution Tools ---
# These give the scrum master step-by-step control over the sprint,
# with reasoning between each action.


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_next_task() -> str:
    """Get the next task to work on (first incomplete ralph-owned task with deps met).

    Returns:
        JSON with task details (id, title, description, acceptance_criteria, files,
        depends_on, complexity, epic) or null if no task is ready.
    """
    tracker = _make_task_tracker()
    task = tracker.get_next_task()
    if task is None:
        return json.dumps({
            "next_task": None,
            "reason": "No tasks ready (all complete or deps unmet)",
        })

    return json.dumps({"next_task": task}, indent=2)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_start_task(task_id: str, resume: bool = False) -> str:
    """Create/checkout a feature branch for a task and prepare for coding.

    Ensures main is up to date, then creates the branch ralph/{task_id}-{title}.
    If the branch already exists and resume=True, checks it out instead.

    Args:
        task_id: The task ID to start (e.g. M6-06)
        resume: If True, resume on existing branch instead of erroring

    Returns:
        JSON with branch name, had_commits (for resume), and status
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    tracker = _make_task_tracker(logger)
    bm = BranchManager(PROJECT_DIR, runner, logger)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    branch = f"ralph/{task_id}-{bm.sanitise_branch_name(task.get('title', ''))}"

    try:
        bm.ensure_main_up_to_date()
    except Exception as e:
        return json.dumps({"error": f"Failed to sync main: {e}", "branch": branch})

    try:
        status = bm.checkout_or_create(branch, resume)
    except Exception as e:
        return json.dumps({"error": f"Failed to create/checkout branch: {e}", "branch": branch})

    return json.dumps({
        "task_id": task_id,
        "branch": branch,
        "existed": status.existed,
        "had_commits": status.had_commits,
        "status": "ready_for_coder",
    })


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_run_coder(task_id: str, agent: str = "opencode") -> str:
    """Invoke the AI coder to implement a task on the current branch.

    The coder writes code and tests, then commits. The scrum master should
    call rzilla_start_task first to set up the branch.

    Args:
        task_id: The task ID to code
        agent: AI agent to use (opencode, claude, gemini) — default: opencode

    Returns:
        JSON with success bool, coder output summary, and any error
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    config = _make_config()
    tracker = _make_task_tracker(logger)
    ai_runner = AIRunner(runner, logger, config)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    prd = tracker.load()
    prompt = ralph.PromptBuilder.coder_prompt(task, agent, prd, resume=False)

    try:
        success = ai_runner.run_coder(agent, prompt, PROJECT_DIR)
    except Exception as e:
        return json.dumps({"task_id": task_id, "success": False, "error": str(e)})

    return json.dumps({"task_id": task_id, "success": success})


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_run_precommit(task_id: str) -> str:
    """Run pre-commit checks (ruff, lint, format) and optionally fix.

    Args:
        task_id: The task ID being worked on

    Returns:
        JSON with passed bool, rounds_used, and any failures
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    config = _make_config()
    tracker = _make_task_tracker(logger)
    ai_runner = AIRunner(runner, logger, config)
    gate = PreCommitGate(runner, ai_runner, logger, config)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    prd = tracker.load()
    result = gate.run(task, prd, PROJECT_DIR)

    return json.dumps({
        "task_id": task_id,
        "passed": result.passed,
        "rounds_used": result.rounds_used,
    })


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_run_tests(task_id: str) -> str:
    """Run pytest and attempt to fix failures (up to max rounds).

    Args:
        task_id: The task ID being worked on

    Returns:
        JSON with passed bool, rounds_used, and test output
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    config = _make_config()
    tracker = _make_task_tracker(logger)
    ai_runner = AIRunner(runner, logger, config)
    test_runner = TestRunner(runner, ai_runner, tracker, logger, config)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    prd = tracker.load()
    result = test_runner.run(task, prd)

    return json.dumps({
        "task_id": task_id,
        "passed": result.passed,
        "rounds_used": result.rounds_used,
    })


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_push_branch(task_id: str) -> str:
    """Push current branch to origin and create a PR if none exists.

    Args:
        task_id: The task ID being worked on

    Returns:
        JSON with branch, pr_number, pr_url, or error
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    tracker = _make_task_tracker(logger)
    bm = BranchManager(PROJECT_DIR, runner, logger)
    pr_mgr = PRManager(runner, logger)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    branch = f"ralph/{task_id}-{bm.sanitise_branch_name(task.get('title', ''))}"

    try:
        bm.push_branch(branch)
    except Exception as e:
        return json.dumps({"error": f"Push failed: {e}", "branch": branch})

    existing_pr = pr_mgr.get_existing(branch)
    if existing_pr:
        return json.dumps({
            "branch": branch,
            "pr_number": existing_pr.number,
            "pr_url": existing_pr.url,
            "pr_status": "existing",
        })

    try:
        pr_info = pr_mgr.create(branch, task)
    except Exception as e:
        return json.dumps({"error": f"PR creation failed: {e}", "branch": branch})

    return json.dumps({
        "branch": branch,
        "pr_number": pr_info.number,
        "pr_url": pr_info.url,
        "pr_status": "created",
    })


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_run_review(task_id: str, pr_number: int, agent: str = "opencode") -> str:
    """Invoke AI code reviewer on a PR.

    Args:
        task_id: The task ID being reviewed
        pr_number: The PR number to review
        agent: AI agent to use for review (default: opencode)

    Returns:
        JSON with review verdict and review text
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    config = _make_config()
    tracker = _make_task_tracker(logger)
    ai_runner = AIRunner(runner, logger, config)
    review_loop = ralph.ReviewLoop(
        pr_mgr=PRManager(runner, logger),
        ai_runner=ai_runner,
        logger=logger,
        config=config,
    )

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    prd = tracker.load()
    coder, reviewer, _ = ai_runner.assign_agents(agent)

    result = review_loop.run(task, pr_number=pr_number, prd=prd, coder=coder, reviewer=reviewer)

    return json.dumps({
        "task_id": task_id,
        "pr_number": pr_number,
        "verdict": result.verdict if hasattr(result, "verdict") else "unknown",
        "rounds": result.rounds if hasattr(result, "rounds") else 0,
    })


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_wait_ci(pr_number: int, timeout_minutes: int = 30) -> str:
    """Wait for CI checks on a PR and return the result.

    Polls GitHub Actions until CI passes or fails, up to timeout_minutes.

    Args:
        pr_number: The PR number to check CI for
        timeout_minutes: Max wait time in minutes (default: 30)

    Returns:
        JSON with ci_status (PASSED/FAILED/TIMEOUT) and details
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    config = _make_config()
    ai_runner = AIRunner(runner, logger, config)
    ci_poller = ralph.CIPoller(runner, ai_runner, logger, config)

    result = ci_poller.wait_and_fix(
        pr_number=pr_number,
        prd=_make_task_tracker(logger).load(),
        task=_make_task_tracker(logger).get_next_task() or {},
        max_fix_rounds=0,
    )

    return json.dumps({
        "pr_number": pr_number,
        "ci_passed": result.passed if hasattr(result, "passed") else False,
    })


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_merge_task(task_id: str, pr_number: int) -> str:
    """Merge a PR and mark the task complete in prd.json and progress.txt.

    Args:
        task_id: The task ID to mark complete
        pr_number: The PR number to merge

    Returns:
        JSON with merged bool and task completion status
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    tracker = _make_task_tracker(logger)
    pr_mgr = PRManager(runner, logger)
    bm = BranchManager(PROJECT_DIR, runner, logger)

    try:
        pr_mgr.merge(pr_number)
    except Exception as e:
        return json.dumps({"error": f"Merge failed: {e}", "task_id": task_id})

    bm.ensure_main_up_to_date()
    tracker.mark_complete(task_id)
    tracker.append_progress(task_id)

    try:
        tracker.commit_tracking(task_id)
    except Exception as e:
        return json.dumps({"merged": True, "tracking_error": str(e), "task_id": task_id})

    return json.dumps({"merged": True, "task_id": task_id, "pr_number": pr_number})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_commit_partial(task_id: str) -> str:
    """Commit any uncommitted changes on the current branch (rescue from failed coder).

    Use this when the coder fails but has left partial work in the working tree.
    Prevents the next rzilla_start_task from wiping changes via git reset --hard.

    Args:
        task_id: The task ID that failed

    Returns:
        JSON with committed bool and file count
    """
    logger = _make_logger()
    runner = _make_runner(logger)
    tracker = _make_task_tracker(logger)

    task = tracker.get_task_by_id(task_id)
    if task is None:
        return json.dumps({"error": f"Task '{task_id}' not found in prd.json"})

    dirty = runner.run(["git", "status", "--porcelain"], cwd=PROJECT_DIR)
    if not dirty.stdout.strip():
        return json.dumps({"committed": False, "reason": "No uncommitted changes"})

    file_count = len(dirty.stdout.strip().splitlines())
    task_title = task.get("title", "untitled")

    runner.run(["git", "add", "-A"], cwd=PROJECT_DIR, check=True)
    runner.run(
        ["git", "commit", "--no-verify", "-m", f"[{task_id}] {task_title} [coder-failed-partial]"],
        cwd=PROJECT_DIR,
        check=True,
    )

    return json.dumps({
        "committed": True,
        "task_id": task_id,
        "files_rescued": file_count,
        "message": f"Committed {file_count} file(s) as partial work — use --resume to continue",
    })


# --- Legacy Tools (fire-and-forget) ---

@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_dry_run(task: str | None = None) -> str:
    """Run rzilla in dry-run mode to preview what would happen.

    Args:
        task: Optional specific task ID to dry-run (default: None = next pending)

    Returns:
        stdout+stderr from the dry-run command
    """
    cmd = [str(RALPH_DIR / ".venv" / "bin" / "rzilla"), "run", "--dry-run"] + _repo_dir_flag
    if task:
        cmd.extend(["--task", task])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_DIR),
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return output or "(dry-run produced no output)"
    except subprocess.TimeoutExpired:
        return "Dry-run timed out after 30 seconds"
    except FileNotFoundError:
        return "Error: 'uv' command not found. Make sure uv is installed."
    except Exception as e:
        return f"Error running dry-run: {e}"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def rzilla_run(
    task: str | None = None,
    skip_review: bool = False,
    opencode_only: bool = False,
    opencode_model: str | None = None,
    opencode_reviewer_model: str | None = None,
    opencode_test_writer_model: str | None = None,
    resume: bool = False,
    max_iterations: int = 10,
) -> str:
    """Start a rzilla sprint as a detached background process.

    Args:
        task: Optional specific task ID to run (default: None = next pending)
        skip_review: Skip AI review phase (default: False)
        opencode_only: Use only opencode models (default: False)
        opencode_model: Specific opencode coder model to use (default: None)
        opencode_reviewer_model: Specific opencode reviewer model (default: None)
        opencode_test_writer_model: Specific opencode test-writer model (default: None)
        resume: Resume from existing branch (default: False)
        max_iterations: Maximum sprint iterations (default: 10)

    Returns:
        JSON string with pid, message, and log_file path
    """
    cmd = [str(RALPH_DIR / ".venv" / "bin" / "rzilla"), "run"] + _repo_dir_flag

    if task:
        cmd.extend(["--task", task])
    if skip_review:
        cmd.append("--skip-review")
    if opencode_only:
        cmd.append("--opencode-only")
    if opencode_model:
        cmd.extend(["--opencode-model", opencode_model])
    if opencode_reviewer_model:
        cmd.extend(["--opencode-reviewer-model", opencode_reviewer_model])
    if opencode_test_writer_model:
        cmd.extend(["--opencode-test-writer-model", opencode_test_writer_model])
    if resume:
        cmd.append("--resume")
    if max_iterations != 10:
        cmd.extend(["--max", str(max_iterations)])

    try:
        # Open log file for appending
        with open(LOG_FILE, "a", encoding="utf-8") as log_f:
            # Start process in new session (detached from parent)
            process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(PROJECT_DIR),
                start_new_session=True,
            )

            result = {
                "pid": process.pid,
                "message": f"Started rzilla sprint (PID: {process.pid})",
                "log_file": str(LOG_FILE),
                "command": " ".join(cmd),
            }
            return json.dumps(result, indent=2)

    except FileNotFoundError:
        result = {
            "pid": None,
            "message": "Error: 'uv' command not found. Make sure uv is installed.",
            "log_file": str(LOG_FILE),
            "command": " ".join(cmd),
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        result = {
            "pid": None,
            "message": f"Error starting rzilla: {e}",
            "log_file": str(LOG_FILE),
            "command": " ".join(cmd),
        }
        return json.dumps(result, indent=2)


@mcp.tool(annotations={"readOnlyHint": False})
def rzilla_add(spec: str) -> str:
    """Add a new task to the ralphzilla backlog.

    Args:
        spec: Task specification (natural language description or GitHub issue URL)

    Returns:
        Output from the rzilla add command
    """
    cmd = [str(RALPH_DIR / ".venv" / "bin" / "rzilla"), "add", spec] + _repo_dir_flag

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_DIR),
            timeout=60,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return output or "Task added successfully"
    except subprocess.TimeoutExpired:
        return "Command timed out after 60 seconds"
    except FileNotFoundError:
        return "Error: 'uv' command not found. Make sure uv is installed."
    except Exception as e:
        return f"Error adding task: {e}"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def rzilla_abort() -> str:
    """Abort the currently running rzilla sprint.

    Finds the rzilla run process and sends SIGTERM.

    Returns:
        Confirmation message or error if no sprint found
    """
    pid = _get_rzilla_pid()

    if pid is None:
        return "No running rzilla sprint found."

    try:
        os.kill(pid, signal.SIGTERM)
        return f"Sent SIGTERM to rzilla sprint (PID: {pid}). Sprint should terminate gracefully."
    except ProcessLookupError:
        return f"Process (PID: {pid}) not found. It may have already terminated."
    except PermissionError:
        return f"Permission denied when trying to terminate process (PID: {pid})."
    except Exception as e:
        return f"Error aborting sprint: {e}"


# --- Main Entry Point ---

if __name__ == "__main__":
    mcp.run()
