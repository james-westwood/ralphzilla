#!/usr/bin/env python3
"""
ralph_mcp.py — FastMCP server for ralphzilla AI sprint runner.

Exposes 8 MCP tools for monitoring and controlling the ralphzilla sprint loop:
- rzilla_status: Get sprint status overview
- rzilla_tasks: List tasks with filtering
- rzilla_log: Get last N lines of progress log
- rzilla_summary: Get latest sprint summary
- rzilla_dry_run: Run a dry-run simulation
- rzilla_run: Start a sprint as background process
- rzilla_add: Add a new task to the backlog
- rzilla_abort: Abort running sprint

Usage:
    uv run --extra mcp python ralph_mcp.py
    # or via MCP client with .mcp.json configuration
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from pathlib import Path

os.environ["MCP_LOG_LEVEL"] = "ERROR"

import psutil
from mcp.server.fastmcp import FastMCP

REPO_DIR = Path(__file__).parent
PRD_FILE = REPO_DIR / "prd.json"
PROGRESS_FILE = REPO_DIR / "progress.txt"
LOG_FILE = REPO_DIR / "ralph-loop.log"

mcp = FastMCP("rzilla")

# FastMCP.__init__ adds a RichHandler(stderr) to the root logger at INFO
# level, which corrupts the JSON-RPC stdio transport. Remove it and set
# root logger to ERROR to prevent any output leaking to stderr.
logging.root.handlers = [logging.NullHandler()]
logging.root.setLevel(logging.ERROR)


# --- Helper Functions ---

def _read_prd() -> dict:
    """Read and parse prd.json from disk."""
    if not PRD_FILE.exists():
        return {"tasks": []}
    with open(PRD_FILE, encoding="utf-8") as f:
        return json.load(f)


def _find_latest_summary() -> Path | None:
    """Find the most recent ralph-summary-*.md file."""
    summaries = sorted(REPO_DIR.glob("ralph-summary-*.md"))
    return summaries[-1] if summaries else None


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


@mcp.tool(annotations={"readOnlyHint": True})
def rzilla_dry_run(task: str | None = None) -> str:
    """Run rzilla in dry-run mode to preview what would happen.

    Args:
        task: Optional specific task ID to dry-run (default: None = next pending)

    Returns:
        stdout+stderr from the dry-run command
    """
    cmd = ["uv", "run", "rzilla", "run", "--dry-run"]
    if task:
        cmd.extend(["--task", task])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
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
    resume: bool = False,
    max_iterations: int = 10,
) -> str:
    """Start a rzilla sprint as a detached background process.

    Args:
        task: Optional specific task ID to run (default: None = next pending)
        skip_review: Skip AI review phase (default: False)
        opencode_only: Use only opencode models (default: False)
        opencode_model: Specific opencode model to use (default: None)
        resume: Resume from existing branch (default: False)
        max_iterations: Maximum sprint iterations (default: 10)

    Returns:
        JSON string with pid, message, and log_file path
    """
    cmd = ["uv", "run", "rzilla", "run"]

    if task:
        cmd.extend(["--task", task])
    if skip_review:
        cmd.append("--skip-review")
    if opencode_only:
        cmd.append("--opencode-only")
    if opencode_model:
        cmd.extend(["--opencode-model", opencode_model])
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
                cwd=str(REPO_DIR),
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
    cmd = ["uv", "run", "rzilla", "add", spec]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
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
