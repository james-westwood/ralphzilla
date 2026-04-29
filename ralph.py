#!/usr/bin/env python3
"""
ralph.py — AI sprint runner.

Executes prd.json task backlogs via AI agents with autonomous failure recovery.
Each task becomes a git branch, gets coded by an AI agent, reviewed, CI-gated,
and merged — without human intervention for recoverable failures.

Usage:
    ./ralph.py [OPTIONS]
    rzilla [OPTIONS]   # when installed via pipx or uv

Run with --help for full option list.
"""

import ast
import asyncio
import enum
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import click
import httpx
import yaml

# --- Constants ---

DEFAULT_MAX_ITERATIONS = 10
RUN_HISTORY_FILE = ".ralph/run-history.json"
FINAL_LOG_LINES = 50
DEFAULT_MAX_PRECOMMIT_ROUNDS = 2
DEFAULT_MAX_REVIEW_ROUNDS = 2
DEFAULT_MAX_CI_FIX_ROUNDS = 2
DEFAULT_MAX_TEST_FIX_ROUNDS = 2
CI_POLL_INTERVAL_SECS = 30
CI_POLL_MAX_ATTEMPTS = 60  # 30 min total
CI_PENDING_STATES = frozenset({"PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "EXPECTED"})
CI_FAILURE_STATES = frozenset({"FAILURE", "ERROR"})
SUBPROCESS_TIMEOUT_SECS = 3600  # 1 hour — AI coder calls can be slow
GH_TIMEOUT_SECS = 60
GIT_TIMEOUT_SECS = 120
MAIN_BRANCH = "main"
LOG_FILE_NAME = "ralph.log"
PRD_FILE = "prd.json"
PROGRESS_FILE = "progress.txt"
SUMMARY_FILE_PREFIX = "ralph-summary"
DEFAULT_OPENCODE_MODEL = "opencode/big-pickle"
DEFAULT_OPENCODE_REVIEWER_MODEL = "opencode/kimi-k2.5"
DEFAULT_OPENCODE_TEST_WRITER_MODEL = "opencode/minimax-m2.7"
GEMINI_MODEL = "gemini-2.5-pro"
ESCALATIONS_FILE = ".ralph/escalations.json"
MAX_PROMPT_ARG_BYTES = 100_000  # ~100KB — safe CLI arg limit; beyond this write to file
RALPH_PROMPT_FILE = ".ralph_prompt.md"
MAX_RETRIES_PER_BLOCKER = 3
MAX_TOTAL_BLOCKERS_PER_SPRINT = 5


# --- Exception Hierarchy ---
class RalphError(Exception):
    """Base exception for all Ralphzilla errors."""

    pass


class BranchSyncError(RalphError):
    """ff-only pull failed (diverged main)."""

    pass


class BranchExistsError(RalphError):
    """branch exists, resume=False."""

    pass


class RemoteNotSSHError(RalphError):
    """HTTPS remote detected."""

    pass


class CITimeoutError(RalphError):
    """CI didn't finish in 30 min."""

    pass


class CIFailedFatal(RalphError):  # noqa: N818
    """CI still failing after max fix rounds."""

    pass


class PRDGuardViolation(RalphError):  # noqa: N818
    """coder touched prd.json."""

    pass


class CoderFailedError(RalphError):
    """all coder fallbacks exhausted."""

    pass


class ReviewerFailedError(RalphError):
    """all reviewer fallbacks exhausted."""

    pass


class PreflightError(RalphError):
    """missing CLI tool or auth failure."""

    pass


class PlanInvalidError(RalphError):
    """plan-checker found structural violations."""

    pass


class WaveConflictError(RalphError):
    """A wave contains tasks that share files — would cause a race condition."""

    pass


class PrdValidator:
    """Shared validation layer for prd.json tasks.

    Enforces 4 rules:
    1. description >= 100 chars
    2. At least one AC references a file path pattern
    3. No credential strings in ralph-owned tasks
    4. All depends_on IDs exist in all_task_ids
    """

    FILE_PATH_PATTERN = re.compile(r"[\w/]+\.py|tests/")
    CREDENTIAL_PATTERN = re.compile(r"(?i)(password|secret|api.?key|token)")

    def validate(self, task: dict, all_task_ids: set[str]) -> None:
        task_id = task.get("id", "UNKNOWN")

        description = task.get("description", "")
        if len(description) < 100:
            raise PlanInvalidError(f"{task_id}: description too short (< 100 chars)")

        acs = task.get("acceptance_criteria", [])
        has_file_ref = any(self.FILE_PATH_PATTERN.search(str(ac)) for ac in acs)
        if not has_file_ref:
            raise PlanInvalidError(
                f"{task_id}: no acceptance criterion contains a file path pattern"
            )

        owner = task.get("owner", "")
        if owner == "ralph":
            if self.CREDENTIAL_PATTERN.search(description):
                raise PlanInvalidError(f"{task_id}: description contains credential string")

        for dep_id in task.get("depends_on", []):
            if dep_id not in all_task_ids:
                raise PlanInvalidError(f"{task_id}: depends_on unknown task '{dep_id}'")


class DependencyCycleError(RalphError):
    """Circular dependency detected in task graph."""

    pass


class DependencyGraph:
    """Builds and queries a DAG from task depends_on fields.

    Used to determine execution order and detect which tasks can run concurrently.
    Internally uses an adjacency list where edges point from dependency → dependent
    (i.e., A → B means A must complete before B).
    """

    def __init__(self) -> None:
        self._graph: dict[str, list[str]] = {}  # node -> list of nodes that depend on it
        self._reverse: dict[str, list[str]] = {}  # node -> list of its dependencies
        self._task_ids: set[str] = set()  # IDs explicitly declared as tasks

    def build_graph(self, tasks: list[dict]) -> None:
        """Parse tasks and populate the adjacency lists."""
        self._graph = {}
        self._reverse = {}
        self._task_ids = set()

        for task in tasks:
            task_id = task["id"]
            self._task_ids.add(task_id)
            self._graph.setdefault(task_id, [])
            self._reverse.setdefault(task_id, [])

        for task in tasks:
            task_id = task["id"]
            for dep_id in task.get("depends_on", []):
                self._graph.setdefault(dep_id, []).append(task_id)
                self._reverse[task_id].append(dep_id)

    def validate_dependencies(self) -> list[str]:
        """Return task IDs referenced in depends_on that don't exist in the task list."""
        missing: list[str] = []
        for node, deps in self._reverse.items():
            for dep in deps:
                if dep not in self._task_ids and dep not in missing:
                    missing.append(dep)
        return missing

    def detect_cycles(self) -> bool:
        """Return True if the graph contains a cycle (DFS-based)."""
        white, gray, black = 0, 1, 2
        color: dict[str, int] = {node: white for node in self._graph}

        def dfs(node: str) -> bool:
            color[node] = gray
            for neighbor in self._graph.get(node, []):
                if color.get(neighbor, white) == gray:
                    return True
                if color.get(neighbor, white) == white and dfs(neighbor):
                    return True
            color[node] = black
            return False

        return any(dfs(node) for node in self._graph if color[node] == white)

    def topological_sort(self) -> list[str]:
        """Return task IDs in a valid execution order (dependencies first).

        Raises DependencyCycleError if a cycle is present.
        """
        if self.detect_cycles():
            cycle_nodes = self._find_cycle_nodes()
            raise DependencyCycleError(
                f"Cycle detected among tasks: {', '.join(sorted(cycle_nodes))}"
            )

        # Kahn's algorithm (BFS topological sort)
        in_degree: dict[str, int] = {node: 0 for node in self._graph}
        for node in self._graph:
            for neighbor in self._graph[node]:
                in_degree[neighbor] = in_degree.get(neighbor, 0) + 1

        queue = [node for node, deg in in_degree.items() if deg == 0]
        queue.sort()  # deterministic output
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            neighbors = sorted(self._graph.get(node, []))
            for neighbor in neighbors:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
                    queue.sort()

        return result

    def _find_cycle_nodes(self) -> set[str]:
        """Return the set of node IDs that participate in any cycle."""
        white, gray, black = 0, 1, 2
        color: dict[str, int] = {node: white for node in self._graph}
        cycle_nodes: set[str] = set()

        def dfs(node: str, path: list[str]) -> None:
            color[node] = gray
            path.append(node)
            for neighbor in self._graph.get(node, []):
                if color.get(neighbor, white) == gray:
                    # found a back-edge: record the cycle
                    idx = path.index(neighbor)
                    cycle_nodes.update(path[idx:])
                elif color.get(neighbor, white) == white:
                    dfs(neighbor, path)
            path.pop()
            color[node] = black

        for node in self._graph:
            if color[node] == white:
                dfs(node, [])

        return cycle_nodes


class AgentSandboxViolation(RalphError):  # noqa: N818
    """Agent attempted an operation outside its sandbox."""

    pass


class ReviewerUnavailableError(RalphError):
    """All reviewer agents failed to produce output."""

    pass


# --- Blocker Classification ---
class BlockerKind(enum.Enum):
    MERGE_CONFLICT = enum.auto()
    CI_FATAL = enum.auto()
    PRD_GUARD_VIOLATION = enum.auto()
    REVIEWER_UNAVAILABLE = enum.auto()


# --- Data Classes ---
@dataclass
class Config:
    max_iterations: int
    skip_review: bool
    tdd_mode: bool  # per-sprint TDD flag (--tdd); test writer ≠ coder agent
    model_mode: str  # "random" | "claude" | "gemini" | "opencode"
    opencode_model: str
    resume: bool
    repo_dir: Path
    log_file: Path
    max_precommit_rounds: int
    max_review_rounds: int
    max_ci_fix_rounds: int
    max_test_fix_rounds: int
    max_test_write_rounds: int  # TDD: rounds to get hollow-free tests
    force_task_id: str | None
    deep_review_check: bool = False  # Enable AI meta-review quality check
    claude_only: bool = False
    gemini_only: bool = False
    opencode_only: bool = False
    opencode_reviewer_model: str = DEFAULT_OPENCODE_REVIEWER_MODEL
    opencode_test_writer_model: str = DEFAULT_OPENCODE_TEST_WRITER_MODEL
    validate_plan: bool = False  # Tier 2 AI sanity check on prd.json
    max_workers: int | None = None  # WaveExecutor parallelism cap (None = CPU count)
    workstream: str | None = None  # Optional prefix for worktree branch names


@dataclass
class PRInfo:
    number: int
    url: str


@dataclass
class CIResult:
    passed: bool
    rounds_used: int


@dataclass
class ReviewResult:
    verdict: str  # "APPROVED" | "CHANGES_REQUESTED_MAX_REACHED"
    rounds_used: int


@dataclass
class PreCommitResult:
    passed: bool
    rounds_used: int


@dataclass
class TestResult:
    passed: bool
    rounds_used: int


@dataclass
class BranchStatus:
    existed: bool
    had_commits: bool


@dataclass
class TaskResult:
    fatal: bool
    message: str = ""
    duration: float = 0.0


@dataclass
class WaveSummary:
    """Per-wave execution summary stored in ExecutionReport.wave_histories."""

    wave_number: int
    total: int
    succeeded: int
    failed: int
    skipped: int
    results: dict[str, TaskResult]  # task_id -> TaskResult for all tasks in this wave


@dataclass
class ExecutionReport:
    """Summary of a parallel wave execution run."""

    results: dict[str, TaskResult]  # task_id -> TaskResult
    waves_run: int
    tasks_blocked: list[str] = field(default_factory=list)  # IDs skipped due to failed deps
    wave_histories: list[WaveSummary] = field(default_factory=list)  # per-wave summaries


@dataclass
class ConflictReport:
    """Result of a pre-wave file-overlap conflict check."""

    has_conflicts: bool
    conflicting_tasks: list[tuple[str, str]]  # pairs of task IDs that share a file
    shared_files: dict[str, list[str]]  # file path -> list of task IDs that claim it


class ConflictDetector:
    """Detects file-overlap conflicts between tasks that would run in the same wave.

    Two tasks conflict when they both list the same path in their ``files`` field.
    Running them concurrently risks race conditions where both agents modify the
    same file simultaneously.
    """

    def check_wave_conflicts(self, tasks: list[dict]) -> ConflictReport:
        """Analyse *tasks* for file-path overlaps.

        Args:
            tasks: List of task dicts, each optionally containing a ``files``
                   key with a list of file paths.

        Returns:
            A :class:`ConflictReport` describing any overlaps found.
        """
        # Build a map: file_path -> [task_ids that claim it]
        file_to_tasks: dict[str, list[str]] = {}
        for task in tasks:
            task_id = task["id"]
            for path in task.get("files", []):
                file_to_tasks.setdefault(path, []).append(task_id)

        # Keep only files claimed by more than one task
        shared_files = {p: ids for p, ids in file_to_tasks.items() if len(ids) > 1}

        # Collect unique conflicting pairs (ordered so tests are deterministic)
        seen: set[tuple[str, str]] = set()
        conflicting_tasks: list[tuple[str, str]] = []
        for ids in shared_files.values():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pair = (ids[i], ids[j])
                    if pair not in seen:
                        seen.add(pair)
                        conflicting_tasks.append(pair)

        return ConflictReport(
            has_conflicts=bool(shared_files),
            conflicting_tasks=conflicting_tasks,
            shared_files=shared_files,
        )


class WaveExecutor:
    """Executes tasks in dependency-ordered waves with asyncio concurrency.

    Uses DependencyGraph to group tasks into waves where all dependencies of
    every task in a wave are satisfied by earlier waves.  Tasks within a wave
    are run concurrently via asyncio.gather(); a semaphore caps parallelism to
    max_workers.

    A task_runner callable is injected at construction time so that the class
    is fully unit-testable without touching real git / AI infrastructure.  The
    runner may be a plain sync function or an async coroutine function — both
    are supported.
    """

    def __init__(
        self,
        tasks: list[dict],
        task_runner: Callable[[str], TaskResult] | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._tasks: dict[str, dict] = {t["id"]: t for t in tasks}
        self._graph = DependencyGraph()
        self._graph.build_graph(tasks)
        self._task_runner: Callable[[str], TaskResult] = task_runner or self._default_runner
        self._max_workers: int = max_workers or os.cpu_count() or 4
        self._conflict_detector = ConflictDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_waves(self, task_ids: list[str]) -> list[list[str]]:
        """Group task_ids into sequentially-ordered waves.

        Wave 0 contains tasks with no intra-list dependencies.  Wave N
        contains tasks whose dependencies are all satisfied by waves < N.
        Dependencies on task IDs *outside* task_ids are treated as already
        satisfied (completed externally).

        Within each dependency wave, tasks that share files are split into
        separate sub-waves to prevent parallel file-modification race conditions.
        """
        task_id_set = set(task_ids)
        completed: set[str] = set()
        remaining = list(task_ids)
        waves: list[list[str]] = []

        while remaining:
            ready = sorted(
                tid
                for tid in remaining
                if all(
                    dep not in task_id_set or dep in completed
                    for dep in self._tasks.get(tid, {}).get("depends_on", [])
                )
            )
            if not ready:
                # Unresolvable subset (cycle or missing deps) — emit as a
                # final wave so callers get all task IDs back.
                waves.extend(self._split_conflicting([tid for tid in sorted(remaining)]))
                break
            waves.extend(self._split_conflicting(ready))
            completed.update(ready)
            remaining = [tid for tid in remaining if tid not in set(ready)]

        return waves

    def _split_conflicting(self, task_ids: list[str]) -> list[list[str]]:
        """Split *task_ids* into sub-waves so no two conflicting tasks share a wave.

        Uses a greedy graph-colouring approach: assign each task (in sorted
        order) to the first existing sub-wave that contains none of its
        conflicting peers.  Returns a list of sub-waves (each a sorted list).
        """
        tasks = [self._tasks[tid] for tid in task_ids if tid in self._tasks]
        # Include tasks not in self._tasks (e.g. external deps) as file-less stubs
        task_map = {tid: self._tasks.get(tid, {"id": tid}) for tid in task_ids}
        tasks = [task_map[tid] for tid in task_ids]

        report = self._conflict_detector.check_wave_conflicts(tasks)
        if not report.has_conflicts:
            return [task_ids]

        # Build adjacency set: task_id -> set of task_ids it conflicts with
        conflicts: dict[str, set[str]] = {tid: set() for tid in task_ids}
        for a, b in report.conflicting_tasks:
            conflicts[a].add(b)
            conflicts[b].add(a)

        sub_waves: list[list[str]] = []
        for tid in task_ids:
            placed = False
            for sub_wave in sub_waves:
                if not any(peer in conflicts[tid] for peer in sub_wave):
                    sub_wave.append(tid)
                    placed = True
                    break
            if not placed:
                sub_waves.append([tid])

        return sub_waves

    def execute_wave(self, wave: list[str]) -> dict[str, TaskResult]:
        """Run all tasks in *wave* concurrently and return their results.

        Uses asyncio.gather() with a semaphore capped at max_workers.

        Raises:
            WaveConflictError: if any two tasks in *wave* share a file path,
                which would risk a parallel file-modification race condition.
        """
        wave_tasks = [self._tasks[tid] for tid in wave if tid in self._tasks]
        report = self._conflict_detector.check_wave_conflicts(wave_tasks)
        if report.has_conflicts:
            pairs = ", ".join(f"({a}, {b})" for a, b in report.conflicting_tasks)
            raise WaveConflictError(
                f"wave contains tasks with overlapping files — would cause race conditions. "
                f"Conflicting pairs: {pairs}"
            )
        return asyncio.run(self._execute_wave_async(wave))

    def run_parallel(self, tasks: list[dict]) -> ExecutionReport:
        """Execute all tasks across dependency-ordered waves.

        Failed tasks in a wave do not prevent other tasks in that wave from
        running, but they block any dependent tasks in future waves.
        """
        task_ids = [t["id"] for t in tasks]
        waves = self.build_waves(task_ids)
        all_results: dict[str, TaskResult] = {}
        failed_ids: set[str] = set()
        blocked_ids: list[str] = []
        wave_histories: list[WaveSummary] = []

        for wave_number, wave in enumerate(waves, start=1):
            runnable: list[str] = []
            wave_skipped: list[str] = []
            for tid in wave:
                deps = self._tasks.get(tid, {}).get("depends_on", [])
                if any(dep in failed_ids for dep in deps):
                    blocked_ids.append(tid)
                    wave_skipped.append(tid)
                    all_results[tid] = TaskResult(fatal=True, message="blocked: dependency failed")
                else:
                    runnable.append(tid)

            # Blocked tasks are also failures for dependency propagation
            failed_ids.update(wave_skipped)

            wave_results: dict[str, TaskResult] = {}
            if runnable:
                wave_results = self.execute_wave(runnable)
                all_results.update(wave_results)
                for tid, result in wave_results.items():
                    if result.fatal:
                        failed_ids.add(tid)

            # Include skipped tasks in wave results for summary reporting
            for tid in wave_skipped:
                wave_results[tid] = all_results[tid]

            self.print_wave_summary(wave_results, wave_number, wave_skipped)
            succeeded = sum(1 for tid, r in wave_results.items() if not r.fatal)
            failed = sum(
                1 for tid, r in wave_results.items() if r.fatal and tid not in wave_skipped
            )
            wave_histories.append(
                WaveSummary(
                    wave_number=wave_number,
                    total=len(wave),
                    succeeded=succeeded,
                    failed=failed,
                    skipped=len(wave_skipped),
                    results=dict(wave_results),
                )
            )

        return ExecutionReport(
            results=all_results,
            waves_run=len(waves),
            tasks_blocked=blocked_ids,
            wave_histories=wave_histories,
        )

    def print_wave_summary(
        self,
        wave_results: dict[str, TaskResult],
        wave_number: int,
        skipped_ids: list[str] | None = None,
    ) -> None:
        """Print a human-readable summary of a completed wave to stdout.

        Args:
            wave_results: Mapping of task_id -> TaskResult for all tasks in the wave.
            wave_number:  1-based wave index.
            skipped_ids:  Task IDs that were skipped due to dependency failures.
        """
        skipped_set = set(skipped_ids or [])
        succeeded = [tid for tid, r in wave_results.items() if not r.fatal]
        failed = [tid for tid, r in wave_results.items() if r.fatal and tid not in skipped_set]
        skipped = [tid for tid in wave_results if tid in skipped_set]
        total = len(wave_results)

        header = click.style(f"── Wave {wave_number} complete ", fg="white", bold=True)
        succeeded_txt = click.style(f"{len(succeeded)} succeeded", fg="green")
        failed_txt = click.style(f"{len(failed)} failed", fg="red")
        skipped_txt = click.style(f"{len(skipped)} skipped", fg="yellow")
        click.echo(f"\n{header}({total} tasks: {succeeded_txt}, {failed_txt}, {skipped_txt})")

        for tid in sorted(wave_results):
            result = wave_results[tid]
            duration_str = f"{result.duration:.1f}s"
            if tid in skipped_set:
                symbol = click.style("⊘", fg="yellow")
                status = click.style("skipped", fg="yellow")
                click.echo(f"  {symbol} {tid}  {status}  {duration_str}")
            elif result.fatal:
                symbol = click.style("✗", fg="red")
                status = click.style("failed", fg="red")
                err = f"  — {result.message}" if result.message else ""
                click.echo(f"  {symbol} {tid}  {status}  {duration_str}{err}")
            else:
                symbol = click.style("✓", fg="green")
                status = click.style("ok", fg="green")
                click.echo(f"  {symbol} {tid}  {status}  {duration_str}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_runner(task_id: str) -> TaskResult:  # pragma: no cover
        """Placeholder runner — callers should inject a real runner."""
        return TaskResult(fatal=False, message=f"noop:{task_id}")

    async def _execute_wave_async(self, wave: list[str]) -> dict[str, TaskResult]:
        semaphore = asyncio.Semaphore(self._max_workers)
        coros = [self._run_with_semaphore(tid, semaphore) for tid in wave]
        pairs = await asyncio.gather(*coros)
        return dict(pairs)

    async def _run_with_semaphore(
        self, task_id: str, semaphore: asyncio.Semaphore
    ) -> tuple[str, TaskResult]:
        async with semaphore:
            runner = self._task_runner
            t0 = time.monotonic()
            if asyncio.iscoroutinefunction(runner):
                result = await runner(task_id)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, runner, task_id)
            result.duration = time.monotonic() - t0
            return task_id, result


@dataclass
class PlanCheckResult:
    valid: bool
    errors: list[str]  # structural violations (block sprint start)
    warnings: list[str]  # AI-flagged issues (log but don't block)
    tasks_checked: int
    decompositions: int  # number of tasks auto-decomposed into subtasks


@dataclass
class TestQualityResult:
    passed: bool
    hollow_tests: list[str]  # test names that failed quality checks
    deterministic_issues: list[str]  # ast-detected problems
    ai_issues: list[str]  # AI-flagged semantic hollowness
    rounds_used: int


@dataclass
class CleanExitResult:
    clean: bool
    has_sprint_complete: bool = False
    has_progress_update: bool = False
    no_traceback: bool = True
    missing_markers: list[str] = field(default_factory=list)
    fatal_error_type: str | None = None


@dataclass
class ReviewQualityResult:
    acceptable: bool
    reason: str  # why it failed quality check (if it did)


@dataclass
class TaskExecutionResult:
    task_id: str
    title: str
    pr_number: int | None
    ci_passed: bool
    ci_rounds_used: int
    escalated: bool
    fatal_error_type: str | None
    fatal_error_reason: str | None


@dataclass
class ProjectSpec:
    description: str
    language: str
    runtime: str
    package_manager: str
    test_framework: str
    coverage_tool: str
    quality_checks: list[str]
    human_steps: list[str]
    out_of_scope: list[str]


class DiscoveryWizard:
    """
    Interactive wizard that asks 6 questions to gather project metadata.
    Produces a ProjectSpec dataclass. No AI calls — pure interactive I/O.
    """

    def __init__(self, io_in, io_out):
        self.io_in = io_in
        self.io_out = io_out

    def _prompt(self, question: str) -> str:
        """Display a question and return the user's response."""
        self.io_out.write(question + "\n")
        self.io_out.flush()
        return self.io_in.readline().strip()

    def run(self) -> ProjectSpec:
        """Ask exactly 6 questions in order, return ProjectSpec."""
        self.io_out.write("=" * 50 + "\n")
        self.io_out.write("Ralph Project Discovery\n")
        self.io_out.write("=" * 50 + "\n\n")
        self.io_out.flush()

        q1 = self._prompt(
            "1. One-sentence product description:\n"
            "   (e.g., 'A CLI tool for automating code reviews')\n"
            "> "
        )
        if not q1:
            raise RalphError("Product description cannot be empty")

        q2 = self._prompt(
            "2. Language, runtime, package manager (comma-separated):\n"
            "   (e.g., 'python, 3.13+, uv')\n"
            "> "
        )
        if not q2:
            raise RalphError("Language/runtime/package manager cannot be empty")
        parts = [p.strip() for p in q2.split(",")]
        language = parts[0] if parts else ""
        runtime = parts[1] if len(parts) > 1 else ""
        package_manager = parts[2] if len(parts) > 2 else parts[-1] if parts else ""

        q3 = self._prompt(
            "3. Test framework and coverage tool (comma-separated):\n"
            "   (e.g., 'pytest, pytest-cov')\n"
            "> "
        )
        if not q3:
            raise RalphError("Test framework cannot be empty")
        parts3 = [p.strip() for p in q3.split(",")]
        test_framework = parts3[0] if parts3 else ""
        coverage_tool = parts3[1] if len(parts3) > 1 else ""

        q4 = self._prompt(
            "4. Quality gate commands (one per line, empty to skip):\n"
            "   (e.g., 'uv run pytest tests/ -v')\n"
            "   Press Enter on an empty line when done.\n"
            "> "
        )
        quality_checks = []
        if q4.strip():
            quality_checks.append(q4.strip())
            while True:
                extra = self._prompt("  > ")
                if not extra.strip():
                    break
                quality_checks.append(extra.strip())

        q5 = self._prompt(
            "5. Human-only steps (credentials, infra - one per line, empty to skip):\n"
            "   These become owner:'human' placeholder tasks.\n"
            "   Press Enter on an empty line when done.\n"
            "> "
        )
        human_steps = []
        if q5.strip():
            human_steps.append(q5.strip())
            while True:
                extra = self._prompt("  > ")
                if not extra.strip():
                    break
                human_steps.append(extra.strip())

        q6 = self._prompt(
            "6. What is explicitly out of scope (one per line, empty to skip):\n"
            "   Press Enter on an empty line when done.\n"
            "> "
        )
        out_of_scope = []
        if q6.strip():
            out_of_scope.append(q6.strip())
            while True:
                extra = self._prompt("  > ")
                if not extra.strip():
                    break
                out_of_scope.append(extra.strip())

        return ProjectSpec(
            description=q1,
            language=language,
            runtime=runtime,
            package_manager=package_manager,
            test_framework=test_framework,
            coverage_tool=coverage_tool,
            quality_checks=quality_checks,
            human_steps=human_steps,
            out_of_scope=out_of_scope,
        )


class ReviewQualityChecker:
    """
    Two-tier validation of reviews before the ReviewLoop acts on them.
    Tier 1: Deterministic checks (review length, verdict pattern, file refs, uniqueness).
    Tier 2: AI meta-review (runs only if Tier 1 passes and enabled).
    """

    def __init__(self, ai_runner: "AIRunner", logger: "RalphLogger", config: Config):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def check(self, review_text: str, previous_reviews: list[str]) -> ReviewQualityResult:
        """
        Runs Tier 1 deterministic checks.
        Returns ReviewQualityResult(acceptable: bool, reason: str).
        """
        word_count = len(review_text.split())
        if word_count < 80:
            return ReviewQualityResult(False, f"review too short ({word_count} words)")

        verdict_pattern = r"APPROVED|CHANGES\s+REQUESTED"
        if not re.search(verdict_pattern, review_text, re.IGNORECASE):
            return ReviewQualityResult(False, "no verdict found")

        if not re.search(r"\w+\.py:\d+|\w+/\w+\.\w+", review_text):
            return ReviewQualityResult(False, "no file/line references found")

        if previous_reviews and review_text.strip() == previous_reviews[-1].strip():
            return ReviewQualityResult(False, "identical to previous review (rubber-stamping)")

        return ReviewQualityResult(True, "ok")

    def check_deep(self, review_text: str, task: dict) -> ReviewQualityResult:
        """
        Runs Tier 2 AI meta-review.
        Only runs when Tier 1 passes and config.deep_review_check=True.
        """
        if not self.config.deep_review_check:
            return ReviewQualityResult(True, "ok")

        prompt = PromptBuilder.review_quality_prompt(task, review_text)
        response = self.ai_runner.run_reviewer("gemini", prompt)

        if re.search(r"\bPASS\b", response, re.IGNORECASE):
            return ReviewQualityResult(True, "ok")

        reason = "AI meta-review: review not substantive"
        if re.search(r"FAIL", response, re.IGNORECASE):
            match = re.search(r"FAIL[:\s]+(.+)", response, re.IGNORECASE)
            if match:
                reason = f"AI meta-review: {match.group(1).strip()[:100]}"

        return ReviewQualityResult(False, reason)

    def check_with_retry(
        self,
        review_text: str,
        task: dict,
        prd: dict,
        previous_reviews: list[str],
        round_num: int,
    ) -> tuple[ReviewQualityResult, str]:
        """
        Runs quality check, retries with different reviewer on failure.
        Returns (result, retry_agent).
        """
        result = self.check(review_text, previous_reviews)

        if result.acceptable:
            return result, ""

        self.logger.warn(f"Review quality check failed: {result.reason}")
        if self.config.opencode_only:
            available_agents = ["opencode", "gemini", "claude"]
        else:
            available_agents = ["gemini", "opencode", "claude"]
        retry_agent = available_agents[(round_num + 1) % len(available_agents)]
        return result, retry_agent


class ReviewLoop:
    """
    Drives the reviewer agent, parses verdict, invokes coder fix loop on CHANGES REQUESTED.
    Passes every review through ReviewQualityChecker before acting on it.
    """

    def __init__(
        self,
        pr_manager: "PRManager",
        ai_runner: "AIRunner",
        logger: "RalphLogger",
        config: Config,
    ):
        self.pr_manager = pr_manager
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config
        self.quality_checker = ReviewQualityChecker(ai_runner, logger, config)

    def _parse_verdict(self, review_text: str) -> str:
        """
        Parses verdict from review text.
        CHANGES REQUESTED takes precedence if both strings appear.
        If unclear, treats as APPROVED and logs warning.
        """
        if re.search(r"CHANGES\s+REQUESTED", review_text, re.IGNORECASE):
            return "CHANGES_REQUESTED"

        if re.search(r"APPROVED", review_text, re.IGNORECASE):
            return "APPROVED"

        self.logger.warn("Unclear verdict in review — treating as APPROVED")
        return "APPROVED"

    def run(self, task: dict, pr_number: int, prd: dict, coder: str, reviewer: str) -> ReviewResult:
        """
        Main review loop.
        Gets PR diff, sends to reviewer, quality-checks, handles verdict.
        On CHANGES_REQUESTED: invokes coder fix loop, pushes, re-reviews.
        Returns ReviewResult(verdict, rounds_used).
        """
        diff = self.pr_manager.get_diff(pr_number)
        if not diff.strip():
            self.logger.warn("PR diff is empty — skipping review")
            return ReviewResult(verdict="APPROVED", rounds_used=0)

        rounds_used = 0
        current_reviewer = reviewer
        previous_reviews: list[str] = []

        while rounds_used < self.config.max_review_rounds:
            rounds_used += 1
            self.logger.info(
                f"Review round {rounds_used}/"
                f"{self.config.max_review_rounds} with {current_reviewer}"
            )

            prompt = PromptBuilder.reviewer_prompt(task, diff, prd, rounds_used)
            review_text = self.ai_runner.run_reviewer(current_reviewer, prompt)

            if not review_text:
                self.logger.error(f"Reviewer {current_reviewer} returned no output")
                previous_reviews.append("")
                if self.config.opencode_only:
                    _fallback = {"opencode": "gemini", "gemini": "claude", "claude": "opencode"}
                else:
                    _fallback = {"claude": "gemini", "gemini": "opencode", "opencode": "claude"}
                current_reviewer = _fallback.get(current_reviewer, current_reviewer)
                continue

            previous_reviews.append(review_text)

            quality_result, retry_agent = self.quality_checker.check_with_retry(
                review_text, task, prd, previous_reviews, rounds_used
            )

            if not quality_result.acceptable:
                self.logger.warn(
                    f"Review quality failed: {quality_result.reason} — retrying with {retry_agent}"
                )
                current_reviewer = retry_agent
                continue

            verdict = self._parse_verdict(review_text)

            if verdict == "CHANGES_REQUESTED":
                self.logger.info("Verdict: CHANGES_REQUESTED — invoking coder fix loop")

                fix_prompt = PromptBuilder.review_fix_prompt(task, review_text)
                success = self.ai_runner.run_coder(coder, fix_prompt, self.config.repo_dir)

                if not success:
                    self.logger.error("Coder fix loop failed")
                    return ReviewResult(verdict="CHANGES_REQUESTED", rounds_used=rounds_used)

                branch = f"ralph/{task['id']}-{BranchManager.sanitise_branch_name(task['title'])}"

                self.logger.info("Pushing fix and re-reviewing...")
                self.pr_manager.close(pr_number, "Fixed per review feedback — re-reviewing")

                new_pr = self.pr_manager.create(branch, task["title"], PromptBuilder.pr_body(task))
                new_pr_number = new_pr.number

                diff = self.pr_manager.get_diff(new_pr_number)
                current_reviewer = reviewer
                continue

            return ReviewResult(verdict=verdict, rounds_used=rounds_used)

        self.logger.warn(f"Max review rounds ({self.config.max_review_rounds}) reached")
        return ReviewResult(verdict="CHANGES_REQUESTED_MAX_REACHED", rounds_used=rounds_used)


PLAN_CONSENSUS_OUTPUT = "ralph-plan.md"


class PlanConsensus:
    """
    Lightweight Planner + Critic loop (max 3 iterations).
    Produces a work plan from a brief, iterates with Critic feedback until approval.
    """

    def __init__(self, ai_runner: "AIRunner", logger: "RalphLogger", config: Config):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def run(self, brief: str, max_iterations: int = 3) -> str:
        """
        Run Planner-Critic loop.
        Returns the final plan text.
        Writes plan to ralph-plan.md in repo root.
        """
        feedback = ""
        iteration = 0
        final_plan = ""
        final_verdict = "OKAY"

        for iteration in range(1, max_iterations + 1):
            self.logger.info(f"PlanConsensus iteration {iteration}/{max_iterations}")

            planner_prompt = PromptBuilder.planner_prompt(brief, feedback)
            self.logger.info("Invoking Planner agent")
            plan_output = self.ai_runner.run_reviewer("gemini", planner_prompt)

            if not plan_output:
                self.logger.error("Planner produced no output")
                final_plan = plan_output
                final_verdict = "REJECT"
                break

            critic_prompt = PromptBuilder.critic_prompt(plan_output)
            self.logger.info("Invoking Critic agent")
            critic_output = self.ai_runner.run_reviewer("claude", critic_prompt)

            verdict, reason = self._parse_critic(critic_output)

            if verdict == "OKAY":
                final_plan = plan_output
                final_verdict = "OKAY"
                break

            self.logger.info(f"Critic REJECT: {reason}")
            feedback = reason
            final_plan = plan_output
            final_verdict = "REJECT"

        self.logger.info(f"PlanConsensus complete: iteration {iteration}, verdict {final_verdict}")

        plan_text = self._format_plan(final_plan, iteration, final_verdict)
        output_path = self.config.repo_dir / PLAN_CONSENSUS_OUTPUT
        output_path.write_text(plan_text, encoding="utf-8")
        self.logger.info(f"Plan written to {output_path}")

        return plan_text

    def _parse_critic(self, output: str) -> tuple[str, str]:
        """
        Parse critic output.
        REJECT takes precedence if both strings appear.
        Unclear verdict treated as OKAY with warning logged.
        """
        reject_match = re.search(r"REJECT", output, re.IGNORECASE)
        ok_match = re.search(r"OKAY", output, re.IGNORECASE)

        if reject_match:
            reason = output[reject_match.end() :].strip()
            if not reason:
                reason = "unspecified issues"
            return "REJECT", reason

        if ok_match:
            return "OKAY", ""

        self.logger.warn("Critic verdict unclear — treating as OKAY")
        return "OKAY", ""

    def _format_plan(self, plan_json: str, iterations: int, verdict: str) -> str:
        """Format the plan as markdown with metadata header."""
        timestamp = datetime.now().isoformat()
        header = f"""# Work Plan

- **Generated**: {timestamp}
- **Iterations**: {iterations}
- **Verdict**: {verdict}

---

"""
        try:
            tasks = json.loads(plan_json)
            if isinstance(tasks, list):
                body = self._render_markdown_tasks(tasks)
            else:
                body = plan_json
        except json.JSONDecodeError:
            body = plan_json

        return header + body

    def _render_markdown_tasks(self, tasks: list[dict]) -> str:
        """Render tasks as markdown list."""
        lines = []
        for i, task in enumerate(tasks, 1):
            title = task.get("title", "Untitled")
            desc = task.get("description", "")
            acs = task.get("acceptance_criteria", [])
            owner = task.get("owner", "ralph")
            deps = task.get("depends_on", [])

            lines.append(f"### {i}. {title}")
            lines.append("")
            lines.append(f"**Owner**: {owner}")
            if deps:
                lines.append(f"**Depends on**: {', '.join(deps)}")
            lines.append("")
            lines.append(desc)
            lines.append("")
            if acs:
                lines.append("**Acceptance Criteria**:")
                for ac in acs:
                    lines.append(f"- {ac}")
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)


class RalphLogger:
    """
    Dual-stream logger that writes to stdout and a log file simultaneously.
    Fixed-width level prefix. No Python logging module.
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        # Ensure parent directory exists
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        padded_level = level.ljust(5)
        line = f"{timestamp} [{padded_level}] {message}"
        print(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def info(self, message: str) -> None:
        self._log("INFO", message)

    def warn(self, message: str) -> None:
        self._log("WARN", message)

    def error(self, message: str) -> None:
        self._log("ERROR", message)

    def fatal(self, message: str) -> None:
        self._log("FATAL", message)
        sys.exit(1)


class LoopSupervisor:
    """
    Monitors each sprint run for clean-exit verification.
    Cross-checks ralph.log for CLEAN_EXIT_MARKERS after Orchestrator.run() completes.
    Runs ralph.py as a subprocess with lifecycle monitoring.
    """

    SPRINT_COMPLETE_MARKER = "Sprint complete"
    PROGRESS_UPDATE_MARKER = "progress.txt updated"
    TRACEBACK_PATTERNS = ("Traceback", "Unhandled exception")
    ERROR_MARKERS = ("ERROR", "FATAL", "ERROR:", "FATAL:")

    def __init__(
        self,
        logger: RalphLogger,
        log_path: Path,
        progress_path: Path,
        ralph_path: Path | None = None,
    ):
        self.logger = logger
        self.log_path = log_path
        self.progress_path = progress_path
        self.ralph_path = ralph_path or Path(__file__).parent / "ralph.py"
        self._process: subprocess.Popen | None = None

    def run(
        self,
        task_id: str | None = None,
        max_iterations: int = 1,
        resume: bool = False,
        timeout: int = SUBPROCESS_TIMEOUT_SECS,
    ) -> int:
        """
        Runs ralph.py as a subprocess.

        Args:
            task_id: Specific task ID to run (optional)
            max_iterations: Number of sprint iterations
            resume: Resume from existing branch
            timeout: Subprocess timeout in seconds

        Returns:
            Exit code from ralph.py process
        """
        cmd = [
            sys.executable,
            str(self.ralph_path),
            "run",
            "--max",
            str(max_iterations),
        ]
        if task_id:
            cmd.extend(["--task", task_id])
        if resume:
            cmd.append("--resume")

        self.logger.info(f"Starting LoopSupervisor: {' '.join(cmd)}")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            stdout, stderr = self._process.communicate()
            self.logger.error(f"Process timed out after {timeout}s — killed")
            return 1

        exit_code = self._process.returncode

        if stdout:
            for line in stdout.splitlines()[-20:]:
                self.logger.info(f"[ralph.py] {line}")
        if stderr:
            for line in stderr.splitlines()[-20:]:
                self.logger.warn(f"[ralph.py stderr] {line}")

        if exit_code != 0:
            self.logger.error(f"ralph.py exited with code {exit_code}")
            return exit_code

        self.logger.info("ralph.py completed successfully")
        return 0

    def get_exit_code(self) -> int | None:
        """Returns the exit code of the subprocess, or None if still running."""
        if self._process is None:
            return None
        return self._process.returncode

    def parse_log_for_errors(self) -> list[str]:
        """
        Parses the log file for error markers.

        Returns list of error lines containing ERROR or FATAL markers.
        """
        errors = []
        if not self.log_path.exists():
            return errors

        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    for marker in self.ERROR_MARKERS:
                        if marker in line:
                            errors.append(line.strip())
                            break
        except OSError as e:
            self.logger.warn(f"Failed to parse log for errors: {e}")

        return errors

    def monitor(self, poll_interval: int = 5) -> bool:
        """
        Monitors running subprocess health via log file polling.

        Checks for error markers in the log file while process runs.
        Returns True if healthy, False if errors detected.
        """
        if not self._process or self._process.poll() is not None:
            return True

        if self.log_path.exists():
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                for marker in self.ERROR_MARKERS:
                    if marker in content:
                        self.logger.warn(f"Error marker detected in log: {marker}")
                        return False
            except OSError as e:
                self.logger.warn(f"Failed to read log for monitoring: {e}")

        return True

    def detect_hung(self, timeout: int = 300) -> bool:
        """
        Detects hung process by checking last log activity timestamp.

        Args:
            timeout: Seconds of no activity before considering hung

        Returns:
            True if process appears hung, False otherwise
        """
        if not self.log_path.exists():
            return False

        try:
            stat = self.log_path.stat()
            mtime = stat.st_mtime
            now = time.time()
            if now - mtime > timeout:
                self.logger.warn(f"No log activity for {timeout}s — possible hung")
                return True
        except OSError:
            pass

        return False

    def is_running(self) -> bool:
        """Returns True if subprocess is currently running."""
        return self._process is not None and self._process.poll() is None

    def verify_clean_exit(self) -> CleanExitResult:
        """
        Reads ralph.log final 50 lines and checks for clean-exit markers:
        - 'Sprint complete' log line
        - 'progress.txt updated' log line
        - No 'Traceback' or 'Unhandled exception' in final lines

        Returns CleanExitResult with clean status and missing markers.
        """
        if not self.log_path.exists():
            self.logger.warn("ralph.log not found for clean-exit verification")
            return CleanExitResult(
                clean=False,
                missing_markers=["ralph.log not found"],
            )

        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except OSError as e:
            self.logger.warn(f"Failed to read ralph.log: {e}")
            return CleanExitResult(
                clean=False,
                missing_markers=["ralph.log read error"],
                fatal_error_type="log_read_error",
            )

        final_lines = (
            all_lines[-FINAL_LOG_LINES:] if len(all_lines) > FINAL_LOG_LINES else all_lines
        )
        final_text = "".join(final_lines)

        has_sprint_complete = self.SPRINT_COMPLETE_MARKER in final_text
        has_progress_update = self.PROGRESS_UPDATE_MARKER in final_text
        no_traceback = not any(pattern in final_text for pattern in self.TRACEBACK_PATTERNS)

        missing_markers = []
        if not has_sprint_complete:
            missing_markers.append("Sprint complete marker missing")
        if not has_progress_update:
            missing_markers.append("progress.txt update marker missing")
        if not no_traceback:
            missing_markers.append("Traceback/Unhandled exception found in final log lines")

        clean = has_sprint_complete and has_progress_update and no_traceback

        fatal_error_type = None
        if not no_traceback:
            for line in final_lines:
                if "Traceback" in line or "Unhandled exception" in line:
                    fatal_error_type = "traceback_in_logs"
                    break

        result = CleanExitResult(
            clean=clean,
            has_sprint_complete=has_sprint_complete,
            has_progress_update=has_progress_update,
            no_traceback=no_traceback,
            missing_markers=missing_markers,
            fatal_error_type=fatal_error_type,
        )

        if not clean:
            self.logger.warn(f"Clean-exit verification failed: {missing_markers}")

        return result

    def record_run(self, result: CleanExitResult, tasks_completed: int) -> None:
        """
        Appends run entry to .ralph/run-history.json with:
        - timestamp (ISO format)
        - tasks_completed count
        - final_state (clean/unclean)
        - fatal_error_type (if any)
        """
        history_path = self.log_path.parent / RUN_HISTORY_FILE
        history_path.parent.mkdir(parents=True, exist_ok=True)

        history: list[dict] = []
        if history_path.exists():
            try:
                with open(history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.logger.warn("Failed to read existing run history, starting fresh")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "tasks_completed": tasks_completed,
            "final_state": "clean" if result.clean else "unclean",
            "fatal_error_type": result.fatal_error_type,
        }
        history.append(entry)

        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

        self.logger.info(f"Recorded run history: {entry['final_state']} ({tasks_completed} tasks)")


class SubprocessRunner:
    """
    Single wrapper around subprocess.run() used by every component.
    Key constraint: never shell=True.
    """

    def __init__(self, logger: RalphLogger):
        self.logger = logger
        self._active_pids: set[int] = set()

    def kill_active(self) -> None:
        """Kill all tracked active subprocesses by process group."""
        for pid in list(self._active_pids):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                self.logger.warn(f"[SubprocessRunner] Killed process group for PID {pid}")
            except (ProcessLookupError, OSError):
                pass
        self._active_pids.clear()

    def _run_in_new_session(
        self,
        cmd: list[str],
        env: dict,
        timeout: int,
        cwd: "Path | None",
        check: bool,
    ) -> subprocess.CompletedProcess:
        """Spawn in a new process group; kill the whole group on timeout."""
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self._active_pids.add(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                self.logger.warn(
                    f"[SubprocessRunner] Process group for PID {proc.pid} killed after timeout"
                )
            except (ProcessLookupError, OSError):
                pass
            proc.wait()
            self._active_pids.discard(proc.pid)
            raise
        self._active_pids.discard(proc.pid)
        completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
        return completed

    def run(
        self,
        cmd: list[str],
        env_removals: list[str] | None = None,
        timeout: int = SUBPROCESS_TIMEOUT_SECS,
        cwd: "Path | None" = None,
        check: bool = False,
        start_new_session: bool = False,
    ) -> subprocess.CompletedProcess:
        env_removals = env_removals or []
        self.logger.info(f"Running command: {' '.join(cmd)}")

        child_env = os.environ.copy()
        for key in env_removals:
            child_env.pop(key, None)

        if start_new_session:
            return self._run_in_new_session(cmd, child_env, timeout, cwd, check)

        return subprocess.run(
            cmd,
            env=child_env,
            timeout=timeout,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )


class TaskTracker:
    """
    Sole owner of prd.json and progress.txt.
    Every write method does a fresh json.load() — never uses cached state.
    """

    def __init__(
        self,
        prd_path: Path,
        progress_path: Path,
        runner: SubprocessRunner,
        logger: RalphLogger,
        workstream: str | None = None,
    ):
        self.prd_path = prd_path
        self.progress_path = progress_path
        self.runner = runner
        self.logger = logger
        self.workstream = workstream

    def load(self) -> dict:
        """Reads and returns prd.json from disk."""
        with open(self.prd_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_tasks(self, workstream: str | None = None) -> list[dict]:
        """Returns tasks from prd.json, optionally filtered by workstream prefix.

        If *workstream* is provided, only tasks whose ``id`` starts with that
        prefix are returned.  Falls back to ``self.workstream`` when the
        argument is omitted (``None``).
        """
        effective = workstream if workstream is not None else self.workstream
        prd = self.load()
        tasks = prd.get("tasks", [])
        if effective is not None:
            tasks = [t for t in tasks if t["id"].startswith(effective)]
        return tasks

    def _save(self, prd: dict) -> None:
        """Writes prd dict back to prd.json."""
        with open(self.prd_path, "w", encoding="utf-8") as f:
            json.dump(prd, f, indent=2)

    def get_next_task(self) -> dict | None:
        """
        Returns the first task where:
        - completed=false
        - owner != 'human'
        - decomposed != true
        - all depends_on task IDs are completed
        - id starts with self.workstream prefix (when workstream is set)
        """
        prd = self.load()
        all_tasks = prd.get("tasks", [])
        completed_ids = {t["id"] for t in all_tasks if t.get("completed")}

        tasks = all_tasks
        if self.workstream:
            tasks = [t for t in all_tasks if t["id"].startswith(self.workstream)]

        for task in tasks:
            if task.get("completed"):
                continue
            if task.get("owner") == "human":
                continue
            if task.get("decomposed"):
                continue

            depends_on = task.get("depends_on", [])
            if all(dep_id in completed_ids for dep_id in depends_on):
                return task

        return None

    def get_task_by_id(self, task_id: str) -> dict | None:
        prd = self.load()
        for task in prd.get("tasks", []):
            if task["id"] == task_id:
                return task
        return None

    def count_remaining(self) -> int:
        """Counts incomplete ralph-owned non-decomposed tasks in the current workstream."""
        prd = self.load()
        tasks = prd.get("tasks", [])
        if self.workstream:
            tasks = [t for t in tasks if t["id"].startswith(self.workstream)]
        count = 0
        for task in tasks:
            if (
                not task.get("completed")
                and task.get("owner") != "human"
                and not task.get("decomposed")
            ):
                count += 1
        return count

    def get_quality_checks(self) -> list[str]:
        prd = self.load()
        return prd.get("quality_checks", [])

    def mark_complete(
        self,
        task_id: str,
        completed_at: str | None = None,
        pr_number: int | None = None,
    ) -> None:
        """Fresh load, sets completed=true, completed_at timestamp, pr_number, writes back."""
        prd = self.load()
        found = False
        for task in prd.get("tasks", []):
            if task["id"] == task_id:
                if task.get("completed"):
                    raise PRDGuardViolation(
                        f"task {task_id} already marked complete — possible bulk-marking attack"
                    )
                task["completed"] = True
                if completed_at:
                    task["completed_at"] = completed_at
                if pr_number:
                    task["pr_number"] = pr_number
                found = True
                break
        if not found:
            raise PRDGuardViolation(f"task {task_id} not found")
        self._save(prd)

    def append_progress(
        self,
        task_id: str,
        title: str,
        pr_number: int,
        today: str,
        sprint_start_date: str | None = None,
        iteration_count: int = 0,
    ) -> None:
        """Writes progress.txt in human-readable markdown table format.

        The format includes:
        - Header with sprint metadata (start date, iteration count)
        - Markdown table with columns: Epic | Task ID | Title | Status | Completed | PR
        - Status symbols: ✓ (completed), ⚠ (escalated), ⏸ (pending)
        - Relative timestamps (e.g., "2h ago")
        - Visual separator line after every 5 tasks
        """
        prd = self.load()
        tasks = prd.get("tasks", [])
        completed_tasks = [t for t in tasks if t.get("completed")]
        pending_tasks = [t for t in tasks if not t.get("completed")]

        lines: list[str] = []

        header_lines = [
            "# Sprint Progress",
            "",
        ]
        if sprint_start_date:
            header_lines.append(f"**Sprint Start**: {sprint_start_date}")
        header_lines.append(f"**Iteration**: {iteration_count}")
        header_lines.extend(["", "---", ""])
        lines.extend(header_lines)

        table_header = "| Epic | Task ID | Title | Status | Completed | PR |"
        table_sep = "|------|---------|-------|--------|-----------|----|"
        lines.extend([table_header, table_sep])

        all_tasks = list(completed_tasks)
        for task in pending_tasks:
            if task.get("owner") != "human":
                all_tasks.append(task)

        epics: dict[str, list[dict]] = {}
        for task in all_tasks:
            epic = task.get("epic", "UNK")
            epics.setdefault(epic, []).append(task)

        for epic in sorted(epics.keys()):
            epic_tasks = epics[epic]
            for i, task in enumerate(epic_tasks):
                status_symbol = "⏸"
                completed_str = "-"
                pr_str = "-"

                if task.get("completed"):
                    completed_time = task.get("completed_at") or today
                    relative = self._relative_time(completed_time)
                    completed_str = f"{completed_time} ({relative})"

                    if task.get("escalated") or task.get("retry_exhausted"):
                        status_symbol = "⚠"
                    else:
                        status_symbol = "✓"

                    pr_num = task.get("pr_number")
                    if task.get("id") == task_id and pr_number:
                        pr_num = pr_number
                    if pr_num:
                        pr_str = f"#{pr_num}"
                else:
                    status_symbol = "⏸"

                task_id_cell = task.get("id", "")
                title_cell = task.get("title", "")

                row = (
                    f"| {epic} | {task_id_cell} | {title_cell} | "
                    f"{status_symbol} | {completed_str} | {pr_str} |"
                )
                lines.append(row)

                if (i + 1) % 5 == 0 and i < len(epic_tasks) - 1:
                    lines.append("|------|------|------|------|------|------|")

        lines.append("")
        content = "\n".join(lines)

        with open(self.progress_path, "w", encoding="utf-8") as f:
            f.write(content)

    def _relative_time(self, timestamp: str) -> str:
        """Calculates relative time from a timestamp like '2026-04-21'."""
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d")
        except ValueError:
            return "unknown"

        now = datetime.now()
        delta = now - dt

        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                mins = delta.seconds // 60
                return f"{mins}m ago"
            return f"{hours}h ago"
        elif delta.days == 1:
            return "1d ago"
        elif delta.days < 7:
            return f"{delta.days}d ago"
        elif delta.days < 30:
            weeks = delta.days // 7
            return f"{weeks}w ago"
        elif delta.days < 365:
            months = delta.days // 30
            return f"{months}mo ago"
        else:
            years = delta.days // 365
            return f"{years}y ago"

    def commit_tracking(self, task_id: str, title: str) -> None:
        """git add, commit, push for tracking files."""
        self.runner.run(["git", "add", str(self.prd_path), str(self.progress_path)], check=True)
        commit_msg = f"[{task_id}] {title}: mark complete"
        self.runner.run(["git", "commit", "-m", commit_msg], check=True)
        self.runner.run(["git", "push", "origin", MAIN_BRANCH], check=True)

    def add_task(self, task: dict) -> None:
        """Inserts task at end of tasks list, writes back."""
        prd = self.load()
        prd.setdefault("tasks", []).append(task)
        self._save(prd)

    def mark_decomposed(self, task_id: str) -> None:
        """Sets decomposed=true on matching task, writes back."""
        prd = self.load()
        for task in prd.get("tasks", []):
            if task["id"] == task_id:
                task["decomposed"] = True
                break
        self._save(prd)


GITHUB_ISSUE_PATTERN = re.compile(r"github\.com/.+/issues/(\d+)")


class PrdGenerator:
    """
    Generates tasks from natural language spec or GitHub issue URL.
    Used by 'ralph add' command.
    """

    def __init__(
        self,
        ai_runner: "AIRunner",
        task_tracker: TaskTracker,
        validator: PrdValidator,
        runner: SubprocessRunner,
        logger: RalphLogger,
    ):
        self.ai_runner = ai_runner
        self.task_tracker = task_tracker
        self.validator = validator
        self.runner = runner
        self.logger = logger

    def _is_github_issue_url(self, spec: str) -> bool:
        """Returns True if spec matches GitHub issue URL pattern."""
        return bool(GITHUB_ISSUE_PATTERN.search(spec))

    def _fetch_issue_body(self, issue_url: str) -> str:
        """Fetches issue body via gh CLI."""
        match = GITHUB_ISSUE_PATTERN.search(issue_url)
        if not match:
            raise RalphError(f"Could not parse issue number from URL: {issue_url}")

        issue_number = match.group(1)
        self.logger.info(f"Fetching GitHub issue #{issue_number}...")

        result = self.runner.run(
            ["gh", "issue", "view", issue_number, "--json", "title,body"],
            check=True,
        )
        data = json.loads(result.stdout)

        title = data.get("title", "")
        body = data.get("body", "")

        if body:
            return f"{title}\n\n{body}"
        return title

    def _infer_next_epic_prefix(self, prd: dict) -> int:
        """Scans existing task IDs for highest Mx prefix, returns x+1."""
        max_epic = 0
        for task in prd.get("tasks", []):
            tid = task.get("id", "")
            match = re.match(r"^M(\d+)", tid)
            if match:
                num = int(match.group(1))
                if num > max_epic:
                    max_epic = num
        return max_epic + 1

    def generate(self, spec: str) -> list[dict]:
        """Main entry point: generates tasks from spec or URL."""
        prd = self.task_tracker.load()
        existing_tasks = prd.get("tasks", [])
        all_task_ids = {t["id"] for t in existing_tasks}
        next_epic = self._infer_next_epic_prefix(prd)

        if self._is_github_issue_url(spec):
            spec = self._fetch_issue_body(spec)
            self.logger.info("Fetched issue body, generating tasks...")

        prompt = PromptBuilder.prd_generate_prompt(spec, existing_tasks)
        output = self.ai_runner.run_reviewer("gemini", prompt)
        if not output.strip():
            self.logger.info("Gemini unavailable, falling back to opencode...")
            output = self.ai_runner.run_reviewer("opencode", prompt)

        try:
            match = re.search(r"\[\s*{.*}\s*\]", output, re.DOTALL)
            if match:
                tasks = json.loads(match.group(0))
            else:
                tasks = json.loads(output)
        except json.JSONDecodeError as e:
            raise RalphError(f"Failed to parse AI output as JSON: {e}")

        if not isinstance(tasks, list):
            raise RalphError(f"Expected JSON list, got {type(tasks)}")

        if not tasks:
            raise RalphError("No tasks generated")

        assigned_count = 0
        for task in tasks:
            task["id"] = f"M{next_epic}-{assigned_count + 1:02d}"
            task["completed"] = False
            task["owner"] = "ralph"
            task["epic"] = f"M{next_epic}"

            depends_on = task.get("depends_on", [])
            task["depends_on"] = [dep for dep in depends_on if dep in all_task_ids]

            self.validator.validate(task, all_task_ids)
            self.task_tracker.add_task(task)
            assigned_count += 1
            self.logger.info(f"Added task: {task['id']} {task.get('title')}")

        return tasks


class PlanChecker:
    """
    Validates the plan before the sprint starts.
    Structural validation, complexity inference, and auto-decomposition.
    """

    def __init__(
        self,
        task_tracker: TaskTracker,
        ai_runner,
        logger: RalphLogger,
        validator: PrdValidator | None = None,
    ):
        self.task_tracker = task_tracker
        self.ai_runner = ai_runner
        self.logger = logger
        self.validator = validator or PrdValidator()

    def check_structural(self, prd: dict) -> list[str]:
        """Validates required fields, non-empty ACs, and that depends_on IDs exist."""
        errors = []
        required_fields = {
            "id",
            "title",
            "description",
            "acceptance_criteria",
            "owner",
            "completed",
        }  # noqa: E501
        tasks = prd.get("tasks", [])
        all_ids = {t["id"] for t in tasks}

        for task in tasks:
            if task.get("completed"):
                continue

            task_id = task.get("id", "UNKNOWN")
            missing = required_fields - task.keys()
            if missing:
                errors.append(f"{task_id}: missing fields {missing}")

            if not task.get("acceptance_criteria"):
                errors.append(f"{task_id}: acceptance_criteria is empty")
            elif not isinstance(task["acceptance_criteria"], list):
                errors.append(f"{task_id}: acceptance_criteria must be a list")

            for dep in task.get("depends_on", []):
                if dep not in all_ids:
                    errors.append(f"{task_id}: depends_on unknown task '{dep}'")

            try:
                self.validator.validate(task, all_ids)
            except PlanInvalidError as e:
                errors.append(str(e))

        return errors

    def _infer_complexity(self, task: dict) -> int:
        """Scores 1-3 based on AC count, word count, files, and keywords."""
        score = 1
        ac_count = len(task.get("acceptance_criteria", []))
        if ac_count > 4:
            score += 1

        desc = task.get("description", "")
        word_count = len(desc.split())
        if word_count > 80:
            score += 1

        files_count = len(task.get("files", []))
        if files_count > 3:
            score += 1

        keywords = ["refactor", "migrate", "redesign"]
        if any(kw in desc.lower() for kw in keywords):
            score += 1

        return min(max(score, 1), 3)

    def auto_decompose(self, prd: dict) -> int:
        """Breaks complexity-3 tasks into subtasks via AI."""
        count = 0
        tasks = prd.get("tasks", [])
        # Iterate over a copy — task_tracker.add_task() does a fresh load/save of prd.json,
        # but we're working with the in-memory 'prd' dict passed in.
        # TaskTracker is the sole owner of prd.json on disk.

        for task in list(tasks):
            if task.get("completed") or task.get("decomposed") or task.get("parent"):
                continue

            complexity = task.get("complexity") or self._infer_complexity(task)
            if complexity < 3:
                continue

            self.logger.info(f"Auto-decomposing task {task['id']}...")
            subtasks = self.ai_runner.run_decompose(task)
            if not subtasks:
                continue

            for i, sub in enumerate(subtasks):
                sub["id"] = f"{task['id']}{chr(ord('a') + i)}"
                sub["parent"] = task["id"]
                sub["complexity"] = 2
                sub["completed"] = False
                if i > 0:
                    sub["depends_on"] = [f"{task['id']}{chr(ord('a') + i - 1)}"]
                else:
                    sub["depends_on"] = task.get("depends_on", [])

                self.task_tracker.add_task(sub)

            self.task_tracker.mark_decomposed(task["id"])
            count += 1

        return count

    def run(self, prd: dict, ai_check: bool = False) -> PlanCheckResult:
        """Combines structural check and auto_decompose."""
        errors = self.check_structural(prd)
        if errors:
            raise PlanInvalidError("\n".join(errors))

        warnings = []
        if ai_check:
            pending_tasks = [t for t in prd.get("tasks", []) if not t.get("completed")]
            if pending_tasks:
                prompt = PromptBuilder.plan_check_prompt(pending_tasks)
                ai_response = self.ai_runner.run_reviewer("gemini", prompt)
                if ai_response:
                    warnings = self._parse_warnings(ai_response)

        decompositions = self.auto_decompose(prd)

        return PlanCheckResult(
            valid=True,
            errors=[],
            warnings=warnings,
            tasks_checked=sum(1 for t in prd.get("tasks", []) if not t.get("completed")),
            decompositions=decompositions,
        )

    def _parse_warnings(self, ai_response: str) -> list[str]:
        """Parse [WARN] task_id: reason lines from AI response."""
        warnings = []
        for line in ai_response.splitlines():
            match = re.match(r"\[WARN\]\s+(\S+):\s+(.+)", line)
            if match:
                warnings.append(f"{match.group(1)}: {match.group(2)}")
        return warnings


class BranchManager:
    """
    All git operations. SSH-only enforcement, reset --hard sync.
    """

    def __init__(self, repo_dir: Path, runner: SubprocessRunner, logger: RalphLogger):
        self.repo_dir = repo_dir
        self.runner = runner
        self.logger = logger

    def verify_ssh_remote(self) -> None:
        """Raises RemoteNotSSHError if 'git remote get-url origin' is not SSH."""
        result = self.runner.run(["git", "remote", "get-url", "origin"], cwd=self.repo_dir)
        if not result.stdout.strip().startswith("git@"):
            raise RemoteNotSSHError(
                f"HTTPS remote detected: {result.stdout.strip()}. "
                "SSH remote (git@github.com:...) is required for non-interactive pushes."
            )

    def ensure_main_up_to_date(self) -> None:
        """Checks out main, fetches, and resets --hard to origin/main."""
        dirty = self.runner.run(["git", "status", "--porcelain"], cwd=self.repo_dir)
        if dirty.stdout.strip():
            self.logger.warn("[BranchManager] Uncommitted changes before main reset — stashing")
            self.runner.run(["git", "stash", "push", "-m", "ralph-auto-stash"], cwd=self.repo_dir)
        self.runner.run(["git", "checkout", MAIN_BRANCH], cwd=self.repo_dir, check=True)
        self.runner.run(["git", "fetch", "origin", MAIN_BRANCH], cwd=self.repo_dir, check=True)
        self.runner.run(
            ["git", "reset", "--hard", f"origin/{MAIN_BRANCH}"], cwd=self.repo_dir, check=True
        )

    def checkout_or_create(self, branch: str, resume: bool) -> BranchStatus:
        """Handles branch checkout or creation."""
        # Check if branch exists
        result = self.runner.run(["git", "branch", "--list", branch], cwd=self.repo_dir)
        exists = bool(result.stdout.strip())

        if exists:
            if not resume:
                raise BranchExistsError(f"Branch {branch} already exists and resume=False")
            self.logger.info(f"Resuming existing branch: {branch}")
            self.runner.run(["git", "checkout", branch], cwd=self.repo_dir, check=True)
        else:
            self.logger.info(f"Creating new branch: {branch}")
            self.runner.run(["git", "checkout", "-b", branch], cwd=self.repo_dir, check=True)

        # Check if it has commits vs main
        diff_result = self.runner.run(
            ["git", "rev-list", "--count", f"{MAIN_BRANCH}..{branch}"],
            cwd=self.repo_dir,
            check=True,
        )
        had_commits = int(diff_result.stdout.strip()) > 0

        return BranchStatus(existed=exists, had_commits=had_commits)

    def push_branch(self, branch: str) -> None:
        """Pushes branch to origin, ensuring SSH is used."""
        self.verify_ssh_remote()
        self.runner.run(
            ["git", "push", "--set-upstream", "origin", branch], cwd=self.repo_dir, check=True
        )

    def delete_local(self, branch: str, ignore_missing: bool = False) -> None:
        """Deletes local branch."""
        res = self.runner.run(["git", "branch", "-D", branch], cwd=self.repo_dir)
        if res.returncode != 0 and not ignore_missing:
            self.logger.warn(f"Failed to delete local branch {branch}")

    def delete_remote(self, branch: str, ignore_missing: bool = False) -> None:
        """Deletes remote branch."""
        self.verify_ssh_remote()
        res = self.runner.run(["git", "push", "origin", "--delete", branch], cwd=self.repo_dir)
        if res.returncode != 0 and not ignore_missing:
            self.logger.warn(f"Failed to delete remote branch {branch}")

    def merge_and_cleanup(self, branch: str) -> None:
        """Checks out main, updates, and deletes local branch."""
        self.runner.run(["git", "checkout", MAIN_BRANCH], cwd=self.repo_dir, check=True)
        self.ensure_main_up_to_date()
        self.delete_local(branch, ignore_missing=True)

    def sanitise_branch_name(self, title: str) -> str:
        """Replaces non-alphanumeric with hyphens, lowercases, truncates to 40 chars."""
        sanitised = re.sub(r"[^a-zA-Z0-9-]", "-", title)
        return sanitised.lower()[:40].strip("-")


class WorktreeError(RalphError):
    """Failed to create, access, or remove a git worktree."""

    pass


class WorktreeManager:
    """Manages git worktrees for parallel task isolation.

    Each task gets its own worktree at .ralph/worktrees/{task_id}/ with a
    dedicated branch so concurrent tasks never share a working tree.

    Worktrees are always cleaned up after task completion — success or failure.
    The `workstream` prefix is prepended to branch names when set, e.g.:
        feature-{workstream}-{task_id}  (workstream set)
        feature-{task_id}               (no workstream)
    """

    WORKTREES_DIR = ".ralph/worktrees"

    def __init__(
        self,
        repo_dir: Path,
        runner: SubprocessRunner,
        logger: RalphLogger,
        workstream: str | None = None,
    ) -> None:
        self.repo_dir = repo_dir
        self.runner = runner
        self.logger = logger
        self.workstream = workstream

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_worktree(self, task_id: str, base_branch: str) -> Path:
        """Create an isolated git worktree for *task_id* branched from *base_branch*.

        Returns the Path to the worktree directory.
        Raises WorktreeError if the git command fails.
        """
        worktree_path = self._worktree_path(task_id)
        branch_name = self._branch_name(task_id)

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        self.logger.info(
            f"Creating worktree for {task_id}: path={worktree_path} branch={branch_name}"
        )

        result = self.runner.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_branch],
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            raise WorktreeError(f"Failed to create worktree for {task_id}: {result.stderr.strip()}")

        return worktree_path

    def cleanup_worktree(self, task_id: str) -> None:
        """Remove the worktree for *task_id* and prune stale git references.

        Safe to call even if the worktree was never created or was already removed.
        """
        worktree_path = self._worktree_path(task_id)

        self.logger.info(f"Cleaning up worktree for {task_id}: {worktree_path}")

        # Remove the worktree via git so git's internal tracking is updated
        self.runner.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=self.repo_dir,
        )

        # Prune any stale references left behind
        self.runner.run(["git", "worktree", "prune"], cwd=self.repo_dir)

        # Belt-and-braces: remove the directory if git worktree remove left it
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    def list_active_worktrees(self) -> list[str]:
        """Return task IDs for which a worktree directory currently exists."""
        base = self._worktrees_base()
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def make_isolated_runner(
        self,
        inner: Callable[[str, Path], TaskResult],
    ) -> Callable[[str], TaskResult]:
        """Wrap *inner* so each call gets its own worktree.

        *inner* receives (task_id, worktree_path).  The worktree is created
        before the call and removed afterwards regardless of outcome.
        """

        def runner(task_id: str) -> TaskResult:
            worktree_path = self.create_worktree(task_id, MAIN_BRANCH)
            try:
                return inner(task_id, worktree_path)
            finally:
                self.cleanup_worktree(task_id)

        return runner

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _branch_name(self, task_id: str) -> str:
        if self.workstream:
            return f"feature-{self.workstream}-{task_id}"
        return f"feature-{task_id}"

    def _worktree_path(self, task_id: str) -> Path:
        if self.workstream:
            return self.repo_dir / self.WORKTREES_DIR / self.workstream / task_id
        return self.repo_dir / self.WORKTREES_DIR / task_id

    def _worktrees_base(self) -> Path:
        """Returns the base directory for this manager's worktrees."""
        if self.workstream:
            return self.repo_dir / self.WORKTREES_DIR / self.workstream
        return self.repo_dir / self.WORKTREES_DIR


class PRManager:
    """
    All gh pr operations. Parses PR numbers with regex.
    Handles race condition on fresh PRs with retry.
    """

    def __init__(self, runner: SubprocessRunner, logger: RalphLogger):
        self.runner = runner
        self.logger = logger

    def create(self, branch: str, title: str, body: str) -> PRInfo:
        """Runs gh pr create, parses PR number, returns PRInfo."""
        result = self.runner.run(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
            check=True,
        )
        url = result.stdout.strip()
        match = re.search(r"/(\d+)$", url)
        if not match:
            raise RalphError(f"Could not parse PR number from URL: {url}")
        pr_number = int(match.group(1))
        return PRInfo(number=pr_number, url=url)

    def get_existing(self, branch: str) -> PRInfo | None:
        """Returns open PR for branch if one exists."""
        result = self.runner.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url"],
            check=True,
        )
        prs = json.loads(result.stdout)
        if not prs:
            return None
        return PRInfo(number=prs[0]["number"], url=prs[0]["url"])

    def get_diff(self, pr_number: int, retries: int = 5, delay: int = 10) -> str:
        """Retries with delay if diff is empty (fresh PR race condition)."""
        for i in range(retries):
            result = self.runner.run(["gh", "pr", "diff", str(pr_number)], check=True)
            diff = result.stdout
            if diff.strip():
                return diff
            if i < retries - 1:
                self.logger.info(
                    f"PR diff empty, retrying in {delay}s (attempt {i + 1}/{retries})..."
                )
                time.sleep(delay)
        return ""

    def get_diff_for_file(self, pr_number: int, filepath: str) -> str:
        """Returns diff for a single file in the PR."""
        # gh pr diff has no per-file filter; get full diff and extract the relevant section.
        full_diff = self.get_diff(pr_number)
        lines = full_diff.splitlines()
        file_diff = []
        capturing = False
        for line in lines:
            if line.startswith(f"diff --git a/{filepath} b/{filepath}"):
                capturing = True
            elif line.startswith("diff --git"):
                capturing = False

            if capturing:
                file_diff.append(line)
        return "\n".join(file_diff)

    def get_checks(self, pr_number: int) -> list[dict]:
        """Returns parsed JSON from 'gh pr checks --json name,state,conclusion,required'."""
        result = self.runner.run(
            [
                "gh",
                "pr",
                "checks",
                str(pr_number),
                "--json",
                "name,state,conclusion,required",
            ],
            check=False,  # might return non-zero if some checks failed
        )
        if not result.stdout.strip():
            return []
        return json.loads(result.stdout)

    def merge(self, pr_number: int) -> None:
        """Runs squash merge via gh pr merge --squash --auto."""
        self.runner.run(["gh", "pr", "merge", str(pr_number), "--squash", "--auto"], check=True)

    def close(self, pr_number: int, reason: str) -> None:
        """Posts reason as PR comment then closes."""
        self.runner.run(["gh", "pr", "comment", str(pr_number), "--body", reason], check=True)
        self.runner.run(["gh", "pr", "close", str(pr_number)], check=True)


class PRDGuard:
    """
    Pre-merge safety check that aborts if the coder touched prd.json.
    Threshold is 0 — any modification is a violation.
    """

    def __init__(self, pr_manager: PRManager, logger: RalphLogger):
        self.pr_manager = pr_manager
        self.logger = logger

    def check(self, pr_number: int) -> None:
        """Raises PRDGuardViolation if prd.json was modified in the PR."""
        diff = self.pr_manager.get_diff_for_file(pr_number, PRD_FILE)
        if not diff.strip():
            return

        added_lines = [
            line
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]

        if added_lines:
            offending = "\n".join(added_lines)
            raise PRDGuardViolation(
                f"PR #{pr_number} violated PRDGuard: prd.json must not be modified by the coder.\n"
                f"Offending lines:\n{offending}"
            )


@dataclass
class UnblockResult:
    success: bool
    actions_log: list[str]
    escalated: bool = False
    replacement_task_id: str | None = None
    skip_to_next: bool = False
    alternative_model: str | None = None


@dataclass
class BlockerResult:
    kind: BlockerKind
    task_id: str | None
    context: str


class UnblockStrategy:
    """
    Provides specific recovery tactics for each BlockerKind variant.

    For MERGE_CONFLICT: abort merge, reset branch, and re-apply changes.
    For CI_FATAL: create FIX ticket with failure context and skip to next task.
    For PRD_GUARD_VIOLATION: rollback PR, mark task for human review, create replacement task.
    For REVIEWER_UNAVAILABLE: switch to alternative reviewer model or enable skip-review mode.

    Each strategy logs actions taken and returns success/failure status for escalation.
    """

    def __init__(
        self,
        branch_manager: BranchManager,
        pr_manager: PRManager,
        task_tracker: TaskTracker,
        ai_runner: "AIRunner",
        logger: RalphLogger,
    ):
        self.branch_manager = branch_manager
        self.pr_manager = pr_manager
        self.task_tracker = task_tracker
        self.ai_runner = ai_runner
        self.logger = logger

    def execute(
        self,
        blocker: BlockerResult,
        task: dict,
        prd: dict,
    ) -> UnblockResult:
        """Execute the appropriate unblocking strategy based on blocker kind."""
        if blocker.kind == BlockerKind.MERGE_CONFLICT:
            return self._handle_merge_conflict(blocker, task)
        elif blocker.kind == BlockerKind.CI_FATAL:
            return self._handle_ci_fatal(blocker, task)
        elif blocker.kind == BlockerKind.PRD_GUARD_VIOLATION:
            return self._handle_prd_guard_violation(blocker, task, prd)
        elif blocker.kind == BlockerKind.REVIEWER_UNAVAILABLE:
            return self._handle_reviewer_unavailable(blocker, task)
        else:
            return UnblockResult(
                success=False,
                actions_log=[f"Unknown blocker kind: {blocker.kind}"],
                escalated=True,
            )

    def _handle_merge_conflict(
        self,
        blocker: BlockerResult,
        task: dict,
    ) -> UnblockResult:
        """Abort merge, reset branch, and re-apply changes."""
        actions: list[str] = []
        task_id = task.get("id", "unknown")

        actions.append(f"Detected merge conflict for task {task_id}")
        actions.append(f"Context: {blocker.context[:100]}")

        branch = f"ralph/{task_id}-{self.branch_manager.sanitise_branch_name(task['title'])}"

        try:
            actions.append(f"Resetting branch {branch} to main")
            self.branch_manager.runner.run(
                ["git", "checkout", branch],
                cwd=self.branch_manager.repo_dir,
                check=True,
            )
            self.branch_manager.runner.run(
                ["git", "reset", "--hard", f"origin/{MAIN_BRANCH}"],
                cwd=self.branch_manager.repo_dir,
                check=True,
            )
            actions.append("Branch reset to origin/main")

            actions.append("Re-applying changes via AI coder...")
            self._reapply_changes(task)

            actions.append("Merge conflict recovery completed successfully")
            return UnblockResult(
                success=True,
                actions_log=actions,
            )
        except Exception as e:
            actions.append(f"Merge conflict recovery failed: {e}")
            return UnblockResult(
                success=False,
                actions_log=actions,
                escalated=True,
            )

    def _reapply_changes(self, task: dict) -> None:
        """Re-invoke the coder to re-apply changes."""
        prompt = PromptBuilder.coder_prompt(task, "opencode", {}, resume=True)
        self.ai_runner.run_coder("opencode", prompt, self.branch_manager.repo_dir)

    def _handle_ci_fatal(
        self,
        blocker: BlockerResult,
        task: dict,
    ) -> UnblockResult:
        """Create FIX ticket with failure context and skip to next task."""
        actions: list[str] = []
        task_id = task.get("id", "unknown")

        actions.append(f"Detected CI fatal for task {task_id}")
        actions.append(f"Failure context: {blocker.context[:100]}")

        try:
            ticket_title = f"FIX: {task.get('title', 'Untitled')} - CI failure"
            ticket_body = (
                f"CI failure in task {task_id}\n\n"
                f"Failure context:\n{blocker.context}\n\n"
                f"Task description: {task.get('description', '')}\n\n"
                f"Acceptance criteria:\n"
                + "\n".join(f"- {ac}" for ac in task.get("acceptance_criteria", []))
            )
            result = self.branch_manager.runner.run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--title",
                    ticket_title,
                    "--body",
                    ticket_body,
                ],
                check=True,
            )
            actions.append(f"Created FIX ticket: {result.stdout.strip()[:100]}")
            actions.append("Skipping to next task")
            return UnblockResult(
                success=True,
                actions_log=actions,
                skip_to_next=True,
            )
        except Exception as e:
            actions.append(f"Failed to create FIX ticket: {e}")
            return UnblockResult(
                success=False,
                actions_log=actions,
                skip_to_next=True,
                escalated=True,
            )

    def _handle_prd_guard_violation(
        self,
        blocker: BlockerResult,
        task: dict,
        prd: dict,
    ) -> UnblockResult:
        """Rollback PR, mark task for human review, create replacement task."""
        actions: list[str] = []
        task_id = task.get("id", "unknown")

        actions.append(f"Detected PRD guard violation for task {task_id}")
        actions.append(f"Violation: {blocker.context[:100]}")

        pr_number = task.get("pr_number")
        if pr_number:
            try:
                actions.append(f"Closing PR #{pr_number}")
                self.pr_manager.close(
                    pr_number,
                    "PRD violation: prd.json was modified. Closing for human review.",
                )
                actions.append("PR closed and rolled back")
            except Exception as e:
                actions.append(f"Failed to close PR: {e}")

        tasks = prd.get("tasks", [])
        max_epic = 0
        for t in tasks:
            tid = t.get("id", "")
            match = re.match(r"^M(\d+)", tid)
            if match:
                max_epic = max(max_epic, int(match.group(1)))

        replacement_task = {
            "id": f"M{max_epic + 1}-FIX",
            "title": f"REVIEW: {task.get('title', 'Untitled')}",
            "description": (
                f"Human review required for task {task_id}. "
                f"Original task violated PRD guard (coder modified prd.json). "
                f"Original description: {task.get('description', '')}"
            ),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "owner": "human",
            "completed": False,
            "depends_on": task.get("depends_on", []),
            "epic": task.get("epic", f"M{max_epic}"),
        }

        self.task_tracker.add_task(replacement_task)
        actions.append(f"Created replacement task: {replacement_task['id']}")
        actions.append("Marked original task for human review")

        return UnblockResult(
            success=True,
            actions_log=actions,
            replacement_task_id=replacement_task["id"],
            escalated=True,
        )

    def _handle_reviewer_unavailable(
        self,
        blocker: BlockerResult,
        task: dict,
    ) -> UnblockResult:
        """Switch to alternative reviewer model or enable skip-review mode."""
        actions: list[str] = []
        task_id = task.get("id", "unknown")

        actions.append(f"Detected reviewer unavailable for task {task_id}")
        actions.append(f"Context: {blocker.context[:100]}")

        alternative = "gemini"

        actions.append(f"Switching to alternative reviewer: {alternative}")
        actions.append("Reviewer unavailable recovery completed successfully")

        return UnblockResult(
            success=True,
            actions_log=actions,
            alternative_model=alternative,
        )


class BlockerAnalyser:
    """
    Classifies ralph.py exit causes into BlockerKind categories.
    Parses subprocess exit codes, log file patterns, and error markers
    to categorize failures: MERGE_CONFLICT, CI_FATAL, PRD_GUARD_VIOLATION,
    REVIEWER_UNAVAILABLE.
    """

    MERGE_CONFLICT_PATTERNS = (
        r"merge.*conflict",
        r"conflicting files",
        r"Automatic merge failed",
        r"CONFLICT",
    )
    CI_FATAL_PATTERNS = (
        r"CI.*failed.*fatal",
        r"CIFailedFatal",
        r"ci.*still failing",
        r"test quality failed",
    )
    PRD_GUARD_PATTERNS = (
        r"PRDGuardViolation",
        r"prd\.json must not be modified",
        r"prd\.json.*violated",
    )
    REVIEWER_UNAVAILABLE_PATTERNS = (
        r"Reviewer.*returned no output",
        r"reviewer.*failed",
        r"no output from.*reviewer",
    )

    def __init__(self, logger: RalphLogger | None = None):
        self.logger = logger

    def analyse(
        self,
        exit_code: int,
        error_output: str = "",
        task_id: str | None = None,
    ) -> BlockerResult | None:
        """
        Analyse exit code and error output to classify the blocker.
        Returns BlockerResult with kind, task_id, and context.
        Returns None if no classifyable blocker found.
        """
        output = error_output

        if self._matches_patterns(output, self.MERGE_CONFLICT_PATTERNS):
            return BlockerResult(
                kind=BlockerKind.MERGE_CONFLICT,
                task_id=task_id,
                context=self._extract_context(output, "merge conflict"),
            )

        if self._matches_patterns(output, self.CI_FATAL_PATTERNS):
            return BlockerResult(
                kind=BlockerKind.CI_FATAL,
                task_id=task_id,
                context=self._extract_context(output, "CI fatal"),
            )

        if self._matches_patterns(output, self.PRD_GUARD_PATTERNS):
            return BlockerResult(
                kind=BlockerKind.PRD_GUARD_VIOLATION,
                task_id=task_id,
                context=self._extract_context(output, "PRD guard violation"),
            )

        if self._matches_patterns(output, self.REVIEWER_UNAVAILABLE_PATTERNS):
            return BlockerResult(
                kind=BlockerKind.REVIEWER_UNAVAILABLE,
                task_id=task_id,
                context=self._extract_context(output, "reviewer unavailable"),
            )

        if exit_code != 0 and not output:
            if "CIFailedFatal" in str(task_id or ""):
                return BlockerResult(
                    kind=BlockerKind.CI_FATAL,
                    task_id=task_id,
                    context="CI failed after max fix rounds",
                )

        return None

    def _matches_patterns(self, text: str, patterns: tuple) -> bool:
        """Check if text matches any of the patterns (case-insensitive)."""
        lower_text = text.lower()
        for pattern in patterns:
            if re.search(pattern, lower_text, re.IGNORECASE):
                return True
        return False

    def _extract_context(self, output: str, default: str) -> str:
        """Extract error context from output."""
        lines = output.splitlines()
        for line in lines[:5]:
            if len(line.strip()) > 10:
                return line.strip()[:200]
        return default


class EscalationManager:
    """
    Circuit breaker that prevents infinite retry loops.

    Tracks consecutive failures per BlockerKind and total blocker events
    per sprint. Triggers escalation when:
      - consecutive failures for one blocker kind reach max_retries_per_blocker (default 3)
      - total blocker events for the sprint reach max_total_blockers (default 5)

    Escalation actions:
      1. Emits a loud console alert via click.echo
      2. Writes escalation-{timestamp}.md with full context
      3. Appends an entry to .ralph/escalations.json (failure ledger)
      4. Creates a human-owned REVIEW task via TaskTracker
    """

    def __init__(
        self,
        repo_dir: Path,
        task_tracker: "TaskTracker",
        logger: RalphLogger,
        max_retries_per_blocker: int = MAX_RETRIES_PER_BLOCKER,
        max_total_blockers: int = MAX_TOTAL_BLOCKERS_PER_SPRINT,
    ):
        self.repo_dir = repo_dir
        self.task_tracker = task_tracker
        self.logger = logger
        self.max_retries_per_blocker = max_retries_per_blocker
        self.max_total_blockers = max_total_blockers
        # consecutive failure count per blocker kind name
        self._consecutive_failures: dict[str, int] = {}
        # total blocker events recorded this sprint
        self._total_blockers: int = 0

    def record_failure(self, blocker_kind: BlockerKind) -> None:
        """Record a failure event for blocker_kind and increment sprint total."""
        kind_name = blocker_kind.name
        self._consecutive_failures[kind_name] = self._consecutive_failures.get(kind_name, 0) + 1
        self._total_blockers += 1

    def reset_consecutive(self, blocker_kind: BlockerKind) -> None:
        """Reset the consecutive counter for blocker_kind (call after a success)."""
        self._consecutive_failures[blocker_kind.name] = 0

    def should_escalate(self, blocker_kind: BlockerKind) -> bool:
        """Return True if circuit breaker should trip for this blocker kind."""
        consecutive = self._consecutive_failures.get(blocker_kind.name, 0)
        if consecutive >= self.max_retries_per_blocker:
            return True
        if self._total_blockers >= self.max_total_blockers:
            return True
        return False

    def escalate(self, task: dict, blocker: BlockerResult, context: str) -> None:
        """
        Execute full escalation sequence for a stuck task.

        Args:
            task: The prd.json task dict that is stuck.
            blocker: The classified BlockerResult.
            context: Human-readable failure context string.
        """
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        task_id = task.get("id", "unknown")
        kind_name = (
            blocker.kind.name if isinstance(blocker.kind, BlockerKind) else str(blocker.kind)
        )
        consecutive = self._consecutive_failures.get(kind_name, 0)

        # 1. Loud console alert
        separator = "=" * 60
        alert = (
            f"\n{separator}\n"
            f"[ESCALATION] Task {task_id} — {kind_name}\n"
            f"Consecutive failures: {consecutive} / {self.max_retries_per_blocker}\n"
            f"Total sprint blockers: {self._total_blockers} / {self.max_total_blockers}\n"
            f"Context: {context[:200]}\n"
            f"{separator}\n"
        )
        click.echo(alert, err=True)
        self.logger.error(f"[EscalationManager] Escalating {task_id}: {kind_name}")

        # 2. Write escalation markdown
        md_path = self.repo_dir / f"escalation-{timestamp}.md"
        md_content = self._build_markdown(task, blocker, context, timestamp, consecutive)
        md_path.write_text(md_content, encoding="utf-8")
        self.logger.info(f"[EscalationManager] Wrote {md_path.name}")

        # 3. Append to failure ledger
        self._append_to_ledger(task, blocker, context, timestamp, consecutive)

        # 4. Create human-owned REVIEW task
        self._create_review_task(task)

    def _build_markdown(
        self,
        task: dict,
        blocker: BlockerResult,
        context: str,
        timestamp: str,
        consecutive: int,
    ) -> str:
        task_id = task.get("id", "unknown")
        kind_name = (
            blocker.kind.name if isinstance(blocker.kind, BlockerKind) else str(blocker.kind)
        )
        lines = [
            f"# Escalation Report — {task_id}",
            "",
            f"**Timestamp**: {timestamp}",
            f"**Blocker kind**: {kind_name}",
            f"**Task ID**: {task_id}",
            f"**Task title**: {task.get('title', 'Untitled')}",
            f"**Consecutive failures**: {consecutive}",
            f"**Total sprint blockers**: {self._total_blockers}",
            "",
            "## Context",
            "",
            context,
            "",
            "## Task Description",
            "",
            task.get("description", ""),
            "",
            "## Acceptance Criteria",
            "",
        ]
        for ac in task.get("acceptance_criteria", []):
            lines.append(f"- {ac}")
        lines.append("")
        return "\n".join(lines)

    def _append_to_ledger(
        self,
        task: dict,
        blocker: BlockerResult,
        context: str,
        timestamp: str,
        consecutive: int,
    ) -> None:
        ledger_path = self.repo_dir / ESCALATIONS_FILE
        ledger_path.parent.mkdir(parents=True, exist_ok=True)

        ledger: list[dict] = []
        if ledger_path.exists():
            try:
                with ledger_path.open("r", encoding="utf-8") as f:
                    ledger = json.load(f)
            except (json.JSONDecodeError, OSError):
                ledger = []

        kind_name = (
            blocker.kind.name if isinstance(blocker.kind, BlockerKind) else str(blocker.kind)
        )
        entry = {
            "timestamp": timestamp,
            "task_id": task.get("id", "unknown"),
            "task_title": task.get("title", "Untitled"),
            "blocker_kind": kind_name,
            "consecutive_failures": consecutive,
            "total_sprint_blockers": self._total_blockers,
            "context": context[:500],
        }
        ledger.append(entry)

        with ledger_path.open("w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
        self.logger.info(f"[EscalationManager] Updated ledger: {ESCALATIONS_FILE}")

    def _create_review_task(self, task: dict) -> None:
        prd = self.task_tracker.load()
        tasks = prd.get("tasks", [])
        max_epic = 0
        for t in tasks:
            tid = t.get("id", "")
            match = re.match(r"^M(\d+)", tid)
            if match:
                max_epic = max(max_epic, int(match.group(1)))

        task_id = task.get("id", "unknown")
        review_task = {
            "id": f"M{max_epic + 1}-ESC",
            "title": f"REVIEW: {task.get('title', 'Untitled')} (escalated)",
            "description": (
                f"Human review required. EscalationManager triggered for task {task_id}. "
                f"Original description: {task.get('description', '')}"
            ),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "owner": "human",
            "completed": False,
            "depends_on": [],
            "epic": task.get("epic", f"M{max_epic}"),
        }
        self.task_tracker.add_task(review_task)
        self.logger.info(f"[EscalationManager] Created REVIEW task: {review_task['id']}")


class ScrumMaster:
    """
    Post-sprint branch hygiene.

    _post_sprint_cleanup() identifies and deletes stale ralph/* branches
    to prevent branch accumulation that slows git operations and pollutes
    the repository.

    A branch is stale if it has no open PR (which covers: branches with no PR
    at all, branches whose PR was closed without merge, and branches older than
    STALE_DAYS with no activity).  Branches with an open PR are always kept.
    """

    STALE_DAYS = 7
    RALPH_BRANCH_PREFIX = "ralph/"

    def __init__(
        self,
        branch_manager: BranchManager,
        pr_manager: PRManager,
        runner: "SubprocessRunner",
        logger: RalphLogger,
        repo_dir: Path,
    ):
        self.branch_manager = branch_manager
        self.pr_manager = pr_manager
        self.runner = runner
        self.logger = logger
        self.repo_dir = repo_dir

    def _list_local_ralph_branches(self) -> list[str]:
        """Return local branch names matching ralph/*."""
        result = self.runner.run(
            ["git", "branch", "--list", f"{self.RALPH_BRANCH_PREFIX}*"],
            cwd=self.repo_dir,
        )
        branches = []
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ").strip()
            if branch:
                branches.append(branch)
        return branches

    def _branch_age_days(self, branch: str) -> float:
        """Return days since last commit on branch. Returns inf on error."""
        result = self.runner.run(
            ["git", "log", "-1", "--format=%ct", branch],
            cwd=self.repo_dir,
        )
        ts_str = result.stdout.strip()
        if not ts_str:
            return float("inf")
        try:
            return (time.time() - int(ts_str)) / 86400
        except ValueError:
            return float("inf")

    def _post_sprint_cleanup(self) -> list[str]:
        """
        Delete stale ralph/* branches after sprint completion.

        Stale = no open PR (covers: no PR at all, PR closed without merge,
        or no activity beyond STALE_DAYS).  Branches with an open PR are kept.

        Returns list of deleted branch names.
        """
        branches = self._list_local_ralph_branches()
        deleted: list[str] = []

        for branch in branches:
            self.logger.info(f"[ScrumMaster] Checking branch: {branch}")

            open_pr = self.pr_manager.get_existing(branch)
            if open_pr is not None:
                self.logger.info(f"[ScrumMaster] Skipping {branch} — open PR #{open_pr.number}")
                continue

            age_days = self._branch_age_days(branch)
            if age_days > self.STALE_DAYS:
                reason = f"no activity for {age_days:.1f} days"
            else:
                reason = "no open PR"

            self.logger.info(f"[ScrumMaster] Deleting stale branch {branch}: {reason}")
            self.branch_manager.delete_local(branch, ignore_missing=True)
            deleted.append(branch)

        self.logger.info(
            f"[ScrumMaster] Cleanup complete. Deleted {len(deleted)} stale branch(es)."
        )
        return deleted


class PromptBuilder:
    """
    Stateless text assembly for all AI prompts.
    """

    @staticmethod
    def _inject_epic_addenda(task: dict, prd: dict) -> str:
        epic = task.get("epic", "")
        addenda = prd.get("epic_addenda", {}).get(epic, "")
        if addenda:
            return f"\n\n**Epic-specific checks ({epic}):**\n{addenda}"
        return ""

    @staticmethod
    def coder_prompt(task: dict, coder: str, prd: dict, resume: bool = False) -> str:
        ac_text = "\n".join(
            [f"{i + 1}. {ac}" for i, ac in enumerate(task.get("acceptance_criteria", []))]
        )
        files_text = ", ".join(task.get("files", []))

        prompt = f"""You are {coder}, an expert software engineer.
Your task is: {task.get("title")}
Description: {task.get("description")}

Acceptance Criteria:
{ac_text}

Files to modify: {files_text}

{PromptBuilder._inject_epic_addenda(task, prd)}

IMPORTANT: Do NOT touch prd.json or progress.txt — the orchestrator
handles all of that after your PR is merged.

IMPORTANT: When finished, commit ALL your changes before exiting:
  git add -A
  git commit -m "feat: {task.get("id")} <short description>"
Do NOT leave changes uncommitted — the orchestrator cannot create a PR without commits.
"""
        if resume:
            prompt += """
IMPORTANT: This branch already has commits. Run `git log --oneline` and
`git diff origin/main...HEAD` to see what is already implemented.
Do NOT re-implement work that is already committed.
"""
        return prompt

    @staticmethod
    def precommit_fix_prompt(task: dict, precommit_output: str) -> str:
        return f"""The task '{task.get("title")}' failed pre-commit hooks.
Failure output:
{precommit_output}

Please fix the issues.
"""

    @staticmethod
    def test_fix_prompt(task: dict, test_output: str) -> str:
        return f"""The task '{task.get("title")}' failed quality checks/tests.
Failure output:
{test_output}

Please fix the implementation to pass the tests.
"""

    @staticmethod
    def reviewer_prompt(task: dict, diff: str, prd: dict, round_num: int) -> str:
        title = task.get("title")
        prompt = f"""You are an expert code reviewer.
Review the following diff for task: {title}
Description: {task.get("description")}

Review the diff against these categories:
1. Correctness — logic errors, edge cases, data handling
2. Security — hardcoded secrets, injection, input validation
3. Performance — N+1 queries, unbounded collections
4. Maintainability — functions >50 lines, nesting >4 levels, magic numbers
5. Testing — acceptance criteria from the task are covered; no implementation-testing
6. PRD adherence — implementation matches the task description; nothing out of scope added

{PromptBuilder._inject_epic_addenda(task, prd)}

Diff:
{diff}

Round: {round_num}

Output exactly `APPROVED` or `CHANGES REQUESTED` followed by specific file+line feedback.
Do not output general comments without a file and line number.
"""
        return prompt

    @staticmethod
    def review_fix_prompt(task: dict, review_text: str) -> str:
        return f"""Your PR for task '{task.get("title")}' received feedback:
{review_text}

Please address the requested changes.
"""

    @staticmethod
    def review_quality_prompt(task: dict, review_text: str) -> str:
        acs = "\n".join([f"- {ac}" for ac in task.get("acceptance_criteria", [])])
        return f"""You are reviewing a code review for quality.

Task: {task.get("title")}
Acceptance Criteria:
{acs}

Review Text:
{review_text}

Evaluate whether this review is substantive:
1. Does the review address all the acceptance criteria?
2. Does the review cite specific code (file:line references)?
3. Is the verdict justified by the issues found?

Output exactly PASS if the review is substantive, or FAIL with a brief reason."""

    @staticmethod
    def ci_fix_prompt(task: dict, failure_log: str) -> str:
        return f"""The CI for task '{task.get("title")}' failed.
Last 150 lines of failure log:
{failure_log}

Please fix the implementation to pass the CI.
"""

    @staticmethod
    def pr_body(task: dict) -> str:
        ac_text = "\n".join([f"- [ ] {ac}" for ac in task.get("acceptance_criteria", [])])
        return f"""## Task: {task.get("title")}

{task.get("description")}

### Acceptance Criteria
{ac_text}
"""

    @staticmethod
    def plan_check_prompt(tasks: list[dict]) -> str:
        tasks_text = json.dumps(tasks, indent=2)
        return f"""Review the following sprint plan (tasks):
{tasks_text}

Flag any issues using [WARN]:
- Tasks whose acceptance criteria are untestable ("it works", "looks good")
- Tasks that are not atomic (two or more distinct deliverables in one task)
- Tasks whose description contradicts the acceptance criteria
- Tasks that are ambiguous about what files/modules to touch
"""

    @staticmethod
    def test_writer_prompt(task: dict) -> str:
        ac_text = "\n".join([f"- {ac}" for ac in task.get("acceptance_criteria", [])])
        return f"""Write failing tests for the following task: {task.get("title")}
Description: {task.get("description")}

Acceptance Criteria:
{ac_text}

Write failing tests only. Do NOT implement the module under test.
Tests must fail with ImportError or AssertionError — not pass.
"""

    @staticmethod
    def test_quality_prompt(task: dict, test_source: str, ast_report: str) -> str:
        return f"""Evaluate the quality of the following tests for task: {task.get("title")}
Acceptance Criteria:
{task.get("acceptance_criteria")}

Test Source:
{test_source}

AST Report:
{ast_report}

Does each test genuinely verify its corresponding AC?
Flag any test that checks implementation details instead of observable behaviour,
or that would pass against a trivially wrong implementation.
Output [HOLLOW] <test_name>: <reason> for any issues found.
"""

    @staticmethod
    def decompose_prompt(task: dict) -> str:
        return f"""Break the following complexity-3 task into 2-4 atomic subtasks:
{json.dumps(task, indent=2)}

Output the subtasks as a JSON list of objects with fields:
title, description, acceptance_criteria, files, owner.
"""

    @staticmethod
    def planner_prompt(brief: str, feedback: str = "") -> str:
        feedback_section = ""
        if feedback:
            feedback_section = f"""
## Prior Critic Feedback (address in your revised plan):
{feedback}

"""
        return f"""You are a software architectural planner.
Your task is to produce a detailed work plan from the following brief:

{brief}

{feedback_section}The plan should include:
- A clear list of tasks to implement
- Each task should have: title, description, acceptance criteria
- Acceptance criteria must be measurable and testable
- Tasks must be atomic (one deliverable per task)
- Avoid vague language like "improve", "enhance", "refactor", "fix" without specific outcomes

Output the plan as a JSON list of task objects with these fields:
- title: string
- description: string (detailed, explains what and why)
- acceptance_criteria: list of strings (each testable/measurable)
- owner: string ("ralph" or "human")
- depends_on: list of task IDs (can be empty)

Output ONLY valid JSON — no explanation, no markdown. Start with [ and end with ]."""

    @staticmethod
    def critic_prompt(plan: str) -> str:
        return f"""You are a plan quality critic.
Review the following work plan for quality gates:

{plan}

Evaluate against these criteria:
1. **Measurable ACs** — each task's acceptance criteria must be testable/verifiable
2. **Atomic tasks** — each task has one distinct deliverable
3. **No vague language** — flag any vague verbs without specific outcomes:
   - "improve" → needs specific outcome metric
   - "enhance" → needs specific feature added
   - "refactor" → needs specific structure result
   - "fix" → needs specific bug identifier
   - "optimize" → needs specific performance metric
4. **Clear dependencies** — tasks that depend on each other are properly linked

Output exactly:
- OKAY if the plan passes all gates
- REJECT with specific line-level feedback for any violations

Format for REJECT:
REJECT
- Task N: <specific issue>
- Task M: <specific issue>
"""

    @staticmethod
    def prd_generate_prompt(spec: str, existing_tasks: list[dict]) -> str:
        max_epic = 0
        task_ids = []
        for t in existing_tasks:
            tid = t.get("id", "")
            task_ids.append(tid)
            match = re.match(r"^M(\d+)", tid)
            if match:
                num = int(match.group(1))
                if num > max_epic:
                    max_epic = num

        task_ids_text = "\n".join(task_ids) if task_ids else "(none)"

        return f"""Generate a list of one or more tasks from the following spec:

{spec}

Existing task IDs: {task_ids_text}
Max epic prefix found: M{max_epic}
Next epic prefix should be M{max_epic + 1} for new tasks.

Output a JSON list of task objects with these exact fields:
- id: string (format: M{{next_num}}-01, M{{next_num}}-02, etc.)
- title: string (slug_style_lowercase, max 40 chars, no spaces)
- description: string (detailed, >= 100 chars, explains what and why)
- acceptance_criteria: list of strings (each references a file path like tests/ or ralph.py)
- owner: string ("ralph" - never "human")
- completed: false
- depends_on: list of strings (IDs of OTHER TASKS IN THIS LIST that must complete first, \
plus any existing task IDs that are prerequisites — empty list if none)
- epic: string (M{max_epic + 1})
- complexity: integer (1=simple: single function or constant, implementable in <20min; \
2=medium: one class + tests, implementable in 20-45min; \
3=complex: multiple classes or significant architectural work, >45min)

Task sizing rules — IMPORTANT:
- Prefer complexity 1 or 2. A single roadmap bullet may need to become 2-3 tasks if it is large.
- Each task must be implementable by a single AI coding session without hitting a 15-minute timeout.
- If a deliverable involves multiple distinct classes or subsystems, split it into separate tasks \
  and wire them together with depends_on.
- Set depends_on to include IDs of tasks generated in THIS list when one task builds on another.

Output ONLY valid JSON — no explanation, no markdown formatting. Start with [ and end with ].
"""

    @staticmethod
    def verify_prompt(task: dict, code_context: str) -> str:
        ac_text = "\n".join(
            [f"{i + 1}. {ac}" for i, ac in enumerate(task.get("acceptance_criteria", []))]
        )
        return (
            "You are a code verifier. Evaluate whether each "
            "acceptance criterion is satisfied by the implementation.\n\n"
            f"Task: {task.get('title')}\n"
            f"Description: {task.get('description')}\n\n"
            f"Acceptance Criteria:\n{ac_text}\n\n"
            f"Implementation code:\n{code_context}\n\n"
            "For each criterion, output a line in this EXACT format:\n"
            "N: STATUS: reason\n\n"
            "Where:\n"
            "- N is the criterion number (1, 2, 3, ...)\n"
            "- STATUS is one of: PASSED, FAILED, PARTIAL\n"
            "- reason is a brief explanation of why the criterion "
            "passed, failed, or is partial\n\n"
            "Example:\n"
            "1: PASSED: The function correctly handles edge cases\n"
            "2: FAILED: Missing error handling for null inputs\n"
            "3: PARTIAL: Implemented but only for the happy path\n\n"
            "You MUST evaluate EVERY criterion. Do not skip any."
        )


class RuntimeUnavailableError(Exception):
    """Raised when a requested runtime is not available."""

    pass


@dataclass
class VerifyResult:
    """Result of verifying a task's acceptance criteria."""

    passed: bool = False
    exit_code: int = 1
    verdicts: list[dict] = field(default_factory=list)
    report: str = ""

    def __bool__(self) -> bool:
        return self.passed


def _gather_code_context(files: list[str], repo_dir: Path) -> str:
    if not files:
        default = repo_dir / "ralph.py"
        if default.exists():
            return f"--- ralph.py ---\n{default.read_text(encoding='utf-8', errors='replace')}"
        return "(no files found)"

    parts: list[str] = []
    for filepath in files:
        full_path = repo_dir / filepath
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- {filepath} ---\n{content}")
        else:
            parts.append(f"--- {filepath} ---\n(File not found)")
    return "\n\n".join(parts)


def _build_verify_report(verdicts: list[dict]) -> str:
    symbols = {"PASSED": "\u2713", "FAILED": "\u2717", "PARTIAL": "\u25d0"}
    lines: list[str] = []
    passed_count = sum(1 for v in verdicts if v["status"] == "PASSED")
    failed_count = sum(1 for v in verdicts if v["status"] == "FAILED")
    partial_count = sum(1 for v in verdicts if v["status"] == "PARTIAL")

    for v in verdicts:
        sym = symbols.get(v["status"], "?")
        lines.append(f"  {sym} {v['criterion']} — {v['status']}: {v['reason']}")

    summary = f"Passed: {passed_count}  Failed: {failed_count}  Partial: {partial_count}"
    return f"{summary}\n" + "\n".join(lines)


def _parse_verify_response(response: str, task: dict) -> VerifyResult:
    criteria = task.get("acceptance_criteria", [])
    if not response.strip():
        verdicts = [
            {
                "criterion": c,
                "status": "FAILED",
                "reason": "No response from AI",
            }
            for c in criteria
        ]
        report = _build_verify_report(verdicts)
        return VerifyResult(passed=False, exit_code=1, verdicts=verdicts, report=report)

    verdicts: list[dict] = []
    pattern = re.compile(
        r"(\d+)\s*:\s*(PASSED|FAILED|PARTIAL)\s*:\s*(.+)",
        re.IGNORECASE,
    )
    fallback = re.compile(r"(\d+)\s*:\s*(PASSED|FAILED|PARTIAL)\b", re.IGNORECASE)

    for i, criterion in enumerate(criteria):
        num = str(i + 1)
        matched = False
        for m in pattern.finditer(response):
            if m.group(1) == num:
                verdicts.append(
                    {
                        "criterion": criterion,
                        "status": m.group(2).upper(),
                        "reason": m.group(3).strip(),
                    }
                )
                matched = True
                break
        if matched:
            continue
        for m in fallback.finditer(response):
            if m.group(1) == num:
                verdicts.append(
                    {
                        "criterion": criterion,
                        "status": m.group(2).upper(),
                        "reason": "(no reason provided)",
                    }
                )
                matched = True
                break
        if not matched:
            verdicts.append(
                {
                    "criterion": criterion,
                    "status": "FAILED",
                    "reason": "Could not parse verdict from AI response",
                }
            )

    if not verdicts:
        verdicts = [
            {
                "criterion": c,
                "status": "FAILED",
                "reason": "Unparseable AI response",
            }
            for c in criteria
        ]

    report = _build_verify_report(verdicts)
    all_passed = all(v["status"] == "PASSED" for v in verdicts)
    return VerifyResult(
        passed=all_passed,
        exit_code=0 if all_passed else 1,
        verdicts=verdicts,
        report=report,
    )


@dataclass
class TaskRunResult:
    """Result of running a task with an AI runtime."""

    success: bool = False
    task_id: str = ""
    branch_name: str = ""
    output: str = ""
    agent: str = ""
    error: str = ""
    files_changed: list[str] = field(default_factory=list)


class RuntimeConfig:
    """Configuration for AI runtime selection."""

    SUPPORTED_RUNTIMES = {
        "aider",
        "claude",
        "claude-code",
        "cursor",
        "cline",
        "codex",
        "gemini",
        "opencode",
    }

    def __init__(
        self,
        primary: str,
        fallback: list[str] | None = None,
        timeout: int = 600,
        repo: str = "ralphzilla",
        repo_path: Path | None = None,
        aider_model: str | None = None,
    ):
        if primary not in self.SUPPORTED_RUNTIMES:
            raise ValueError(f"Unsupported runtime: {primary}")

        if fallback:
            for r in fallback:
                if r not in self.SUPPORTED_RUNTIMES:
                    raise ValueError(f"Unsupported fallback runtime: {r}")

        self.primary = primary
        self.fallback = fallback or []
        self.timeout = timeout
        self.repo = repo
        self.repo_path = repo_path or Path.cwd()
        self.aider_model = aider_model


class AIRunnerBase(ABC):
    """Abstract base class for AI runtime implementations."""

    def __init__(
        self,
        runner: SubprocessRunner,
        logger: RalphLogger,
        config: RuntimeConfig,
    ):
        self.runner = runner
        self.logger = logger
        self.config = config

    @abstractmethod
    def run_task(self, task_id: str, branch_name: str) -> TaskRunResult:
        """Execute a task using an AI runtime."""
        pass

    @abstractmethod
    def get_available_runtimes(self) -> set[str]:
        """Return set of available runtime names."""
        pass

    def get_effective_runtime(self) -> str:
        """Return the effective runtime to use, falling back if needed."""
        available = self.get_available_runtimes()
        primary = self.config.primary

        if primary in available:
            return primary

        for fallback in self.config.fallback:
            if fallback in available:
                return fallback

        raise RuntimeUnavailableError(
            f"No runtime available. Primary={primary}, fallback={self.config.fallback}"
        )

    def check_version(self, runtime: str) -> str | None:
        """Check if a runtime is available and return its version."""
        if runtime not in self.get_available_runtimes():
            return None

        if runtime == "opencode":
            try:
                result = self.runner.run(
                    ["opencode", "--version"],
                    capture_output=True,
                )
            except Exception:
                return None
            returncode = getattr(result, "returncode", None)
            if returncode and returncode == 0:
                version = result.stdout.strip()
                version = version.lstrip("v")
                return version.split()[0] if version else None
            elif not returncode:
                version = result.stdout.strip()
                version = version.lstrip("v")
                return version.split()[0] if version else None

        if runtime == "aider":
            try:
                result = self.runner.run(["aider", "--version"], capture_output=True)
            except Exception:
                return None
            returncode = getattr(result, "returncode", None)
            if returncode == 0:
                version = result.stdout.strip() if result.stdout else ""
                parts = version.split()
                return parts[1] if len(parts) > 1 else parts[0]

        return None


class AiderRunner(AIRunnerBase):
    """Aider implementation of AIRunnerBase."""

    def get_available_runtimes(self) -> set[str]:
        try:
            result = self.runner.run(["aider", "--version"], capture_output=True)
            if result.returncode == 0:
                return {"aider"}
        except Exception:
            pass
        return set()

    def run_task(self, task_id: str, branch_name: str) -> TaskRunResult:
        if not self.is_available():
            raise RuntimeUnavailableError("aider runtime not available")

        task_tracker = TaskTracker(self.config.repo_path or Path.cwd())
        task = task_tracker.get_task_by_id(task_id)
        if not task:
            return TaskRunResult(
                success=False,
                task_id=task_id,
                branch_name=branch_name,
                error=f"Task {task_id} not found",
                agent="aider",
            )

        task_url = self._build_task_url(branch_name)
        description = task.get("description", "")
        files = task.get("files", [])
        message = (
            f"Implement task: {task_id}\n{description}\nBranch: {branch_name}\nSee: {task_url}"
        )

        cmd = [
            "aider",
            "--no-auto-commits",
            "--no-git",
            "--yes",
            "--message",
            message,
        ]

        if self.config.aider_model:
            cmd.extend(["--model", self.config.aider_model])

        for f in files:
            cmd.extend(["--file", str(f)])

        files_before = self._get_changed_files()
        try:
            result = self.runner.run(
                cmd,
                cwd=self.config.repo_path,
                timeout=self.config.timeout,
                start_new_session=True,
            )
            output = result.stdout if result else ""
            stderr = (result.stderr if result else "") or ""
            if result and result.returncode != 0:
                output = (output + "\n" + stderr).strip() if stderr else output
                return TaskRunResult(
                    success=False,
                    task_id=task_id,
                    branch_name=branch_name,
                    output=output,
                    error=f"aider exited with code {result.returncode}",
                    agent="aider",
                )
            files_after = self._get_changed_files()
            files_changed = list(files_after - files_before)
            return TaskRunResult(
                success=True,
                task_id=task_id,
                branch_name=branch_name,
                output=output,
                agent="aider",
                files_changed=files_changed,
            )
        except subprocess.TimeoutExpired:
            return TaskRunResult(
                success=False,
                task_id=task_id,
                branch_name=branch_name,
                output="TIMEOUT",
                agent="aider",
                error="Timeout expired",
            )
        except Exception as e:
            return TaskRunResult(
                success=False,
                task_id=task_id,
                branch_name=branch_name,
                error=str(e),
                agent="aider",
            )

    def _build_task_url(self, branch_name: str) -> str:
        """Derive the task URL from git remote origin."""
        try:
            result = self.runner.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.config.repo_path,
                capture_output=True,
            )
            if result and result.returncode == 0:
                url = result.stdout.strip()
                if url.startswith("git@"):
                    url = url.replace("git@", "https://", 1).replace(":", "/", 1)
                if url.endswith(".git"):
                    url = url[:-4]
                return f"{url}/tree/{branch_name}"
        except Exception:
            pass
        return f"https://github.com/james-westwood/{self.config.repo}/tree/{branch_name}"

    def _get_changed_files(self) -> set[str]:
        """Return set of files modified relative to HEAD via git diff."""
        try:
            result = self.runner.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.config.repo_path,
                capture_output=True,
            )
            if result and result.returncode == 0 and result.stdout:
                return {f for f in result.stdout.strip().splitlines() if f.strip()}
        except Exception:
            pass
        return set()

    def is_available(self) -> bool:
        return "aider" in self.get_available_runtimes()


class AIRunner:
    """
    Subprocess wrappers for all AI backends (claude, gemini, opencode).
    Complexity-based model routing. Nested-Claude session detection.
    """

    def __init__(self, runner: SubprocessRunner, logger: RalphLogger, config: Config):
        self.runner = runner
        self.logger = logger
        self.config = config

    def _is_nested_claude_session(self) -> bool:
        """True when ralph is running inside a Claude Code session."""
        return "CLAUDECODE" in os.environ

    def assign_agents(self, task: dict) -> tuple[str, str, str]:
        """Returns (coder, reviewer, test_writer) based on complexity and config."""
        if self.config.claude_only:
            return "claude", "claude", "gemini"
        if self.config.gemini_only:
            return "gemini", "claude", "opencode"
        if self.config.opencode_only:
            return "opencode", "opencode", "opencode"

        complexity = task.get("complexity") or 1
        # Complexity mapping from DESIGN.md
        if self.config.model_mode == "claude":
            return "claude", "gemini", "opencode"
        if self.config.model_mode == "gemini":
            return "gemini", "opencode", "claude"
        if self.config.model_mode == "opencode":
            return "opencode", "gemini", "claude"

        # Default random-ish assignment based on complexity
        if complexity == 1:
            return "opencode", "gemini", "claude"
        elif complexity == 2:
            return "gemini", "claude", "opencode"
        elif complexity >= 3:
            return "claude", "gemini", "opencode"

        return "opencode", "gemini", "claude"

    def _clean_output(self, text: str) -> str:
        """Strips ANSI escape codes and opencode internal UI lines."""
        text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
        text = re.sub(
            r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\x5c)", "", text
        )  # OSC sequences (end with BEL or ESC \)
        text = re.sub(r"\x1b[@-A-Z\x5c-_]", "", text)  # Fe sequences

        ui_prefixes = ("> build", "> session", "> task")
        ui_chars = set("\u2731\u2190\u2192\u2717\u25c7\u25c8\u2713\u25b6\u25c0\u21d2\u2714\u2718")
        filtered = []
        for line in text.splitlines():
            s = line.strip()
            if any(s.startswith(p) for p in ui_prefixes):
                continue
            if s and s[0] in ui_chars:
                continue
            if re.match(r"^\$\s+\S", s):
                continue
            filtered.append(line)

        result = "\n".join(filtered)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def _deliver_prompt(self, prompt: str, cwd: Path) -> tuple[str, "Path | None"]:
        """If prompt exceeds safe CLI arg size, write to a file and return a redirect.

        Returns (effective_prompt, prompt_file_path_or_None).
        Caller must delete the file after the subprocess completes.
        """
        if len(prompt.encode()) <= MAX_PROMPT_ARG_BYTES:
            return prompt, None
        prompt_file = cwd / RALPH_PROMPT_FILE
        prompt_file.write_text(prompt, encoding="utf-8")
        self.logger.warn(
            f"[AIRunner] Prompt too large for CLI arg ({len(prompt.encode())} bytes) "
            f"— written to {RALPH_PROMPT_FILE}"
        )
        redirect = (
            f"Your full task instructions are in the file {RALPH_PROMPT_FILE} "
            "in the current directory. Read that file first, then complete the task."
        )
        return redirect, prompt_file

    def run_coder(
        self,
        agent: str,
        prompt: str,
        cwd: Path,
        *,
        opencode_model_override: str | None = None,
    ) -> bool:
        """Invokes the agent subprocess, returns True on success."""
        self.logger.info(f"Invoking coder: {agent}")
        effective_prompt, prompt_file = self._deliver_prompt(prompt, cwd)
        try:
            if agent == "claude":
                self.runner.run(
                    ["claude", "--dangerously-skip-permissions", "--print", effective_prompt],
                    env_removals=["CLAUDECODE"],
                    cwd=cwd,
                    check=True,
                )
            elif agent == "gemini":
                self.runner.run(
                    ["gemini", "-m", GEMINI_MODEL, "--yolo", "-p", effective_prompt],
                    cwd=cwd,
                    check=True,
                )
            else:  # opencode
                model = opencode_model_override or self.config.opencode_model
                self.runner.run(
                    [
                        "opencode",
                        "run",
                        "-m",
                        model,
                        "--dangerously-skip-permissions",
                        effective_prompt,
                    ],
                    cwd=cwd,
                    check=True,
                    start_new_session=True,
                )
            return True
        except subprocess.CalledProcessError:
            self.logger.error(f"Coder {agent} failed.")
            return False
        finally:
            if prompt_file and prompt_file.exists():
                prompt_file.unlink()

    def run_reviewer(self, agent: str, prompt: str) -> str:
        """Returns reviewer output; handles nested-Claude fallback."""
        if agent == "claude" and self._is_nested_claude_session():
            self.logger.warn(
                "Nested Claude session detected — claude reviewer unavailable."
                " Falling back to gemini."
            )
            return self.run_reviewer("gemini", prompt)

        self.logger.info(f"Invoking reviewer: {agent}")
        _cwd = Path(".")
        effective_prompt, prompt_file = self._deliver_prompt(prompt, _cwd)
        try:
            if agent == "claude":
                result = self.runner.run(
                    ["claude", "--print", effective_prompt],
                    env_removals=["CLAUDECODE"],
                    check=True,
                )
            elif agent == "gemini":
                result = self.runner.run(
                    ["gemini", "-m", GEMINI_MODEL, "-p", effective_prompt],
                    check=True,
                )
            else:  # opencode
                result = self.runner.run(
                    [
                        "opencode",
                        "run",
                        "-m",
                        self.config.opencode_reviewer_model,
                        effective_prompt,
                    ],
                    timeout=300,
                    check=True,
                )
            return self._clean_output(result.stdout)
        except subprocess.CalledProcessError:
            self.logger.error(f"Reviewer {agent} failed.")
            return ""
        finally:
            if prompt_file and prompt_file.exists():
                prompt_file.unlink()

    def run_test_writer(self, prompt: str, cwd: Path, agent: str | None = None) -> bool:
        """Test writer always uses a different model from coder."""
        if agent is None:
            agent = "gemini" if self._is_nested_claude_session() else "claude"
        if agent == "opencode":
            return self.run_coder(
                agent,
                prompt,
                cwd,
                opencode_model_override=self.config.opencode_test_writer_model,
            )
        return self.run_coder(agent, prompt, cwd)

    def run_decompose(self, task: dict) -> list[dict]:
        """AI decompose complexity-3 task."""
        prompt = PromptBuilder.decompose_prompt(task)
        output = self.run_reviewer("claude", prompt)
        try:
            match = re.search(r"\[\s*{.*}\s*\]", output, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return json.loads(output)
        except (json.JSONDecodeError, AttributeError):
            self.logger.error("Failed to parse decomposition output as JSON.")
            return []


def _run_verify(
    task: dict,
    task_tracker: TaskTracker,
    ai_runner: AIRunner,
    repo_dir: Path,
    agent: str | None,
) -> VerifyResult:
    files = task.get("files", [])
    code_context = _gather_code_context(files, repo_dir)
    prompt = PromptBuilder.verify_prompt(task, code_context)

    effective_agent = agent or "gemini"
    response = ai_runner.run_reviewer(effective_agent, prompt)
    return _parse_verify_response(response, task)


class PreCommitGate:
    """
    Runs pre-commit hooks, invokes coder fix loop on failure.
    """

    def __init__(
        self,
        runner: SubprocessRunner,
        ai_runner: AIRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.runner = runner
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def run(self, task: dict, prd: dict, branch_dir: Path) -> PreCommitResult:
        """Runs pre-commit hooks and attempts AI fixes on failure."""
        self.logger.info("Running pre-commit gate...")

        # Auto-fix before AI loop
        self.runner.run(["uv", "run", "ruff", "check", "--fix", "."], cwd=branch_dir)
        self.runner.run(["uv", "run", "ruff", "format", "."], cwd=branch_dir)

        rounds_used = 0
        while rounds_used < self.config.max_precommit_rounds:
            result = self.runner.run(
                ["uv", "run", "pre-commit", "run", "--all-files"], cwd=branch_dir
            )
            if result.returncode == 0:
                return PreCommitResult(passed=True, rounds_used=rounds_used)

            rounds_used += 1
            self.logger.warn(
                f"Pre-commit failed (round {rounds_used}/{self.config.max_precommit_rounds})"
            )

            if rounds_used < self.config.max_precommit_rounds:
                prompt = PromptBuilder.precommit_fix_prompt(task, result.stdout)
                coder, _, _ = self.ai_runner.assign_agents(task)
                self.ai_runner.run_coder(coder, prompt, branch_dir)
            else:
                self.logger.error("Pre-commit still failing after max rounds.")

        return PreCommitResult(passed=False, rounds_used=rounds_used)


class TestRunner:
    """
    Runs quality_checks from prd.json, invokes coder fix loop on failure.
    """

    def __init__(
        self,
        runner: SubprocessRunner,
        ai_runner: AIRunner,
        task_tracker: TaskTracker,
        logger: RalphLogger,
        config: Config,
    ):
        self.runner = runner
        self.ai_runner = ai_runner
        self.task_tracker = task_tracker
        self.logger = logger
        self.config = config

    def run(self, task: dict, prd: dict) -> TestResult:
        """Runs quality checks and attempts AI fixes on failure."""
        self.logger.info("Running quality checks...")
        quality_checks = self.task_tracker.get_quality_checks()

        rounds_used = 0
        while rounds_used < self.config.max_test_fix_rounds:
            all_passed = True
            failure_output = ""

            for cmd_str in quality_checks:
                cmd = cmd_str.split()
                result = self.runner.run(cmd)
                if result.returncode != 0:
                    all_passed = False
                    failure_output += (
                        f"Command failed: {cmd_str}\n{result.stdout}\n{result.stderr}\n"
                    )
                    break

            if all_passed:
                return TestResult(passed=True, rounds_used=rounds_used)

            rounds_used += 1
            self.logger.warn(
                f"Quality checks failed (round {rounds_used}/{self.config.max_test_fix_rounds})"
            )

            if rounds_used < self.config.max_test_fix_rounds:
                prompt = PromptBuilder.test_fix_prompt(task, failure_output)
                coder, _, _ = self.ai_runner.assign_agents(task)
                self.ai_runner.run_coder(coder, prompt, self.config.repo_dir)
            else:
                self.logger.error("Quality checks still failing after max rounds.")

        return TestResult(passed=False, rounds_used=rounds_used)


class RalphTestWriter:
    """
    TDD mode component that invokes a separate AI agent to write failing tests
    before the coder starts. The test writer must be a different model from
    the eventual coder.
    """

    def __init__(
        self,
        ai_runner: "AIRunner",
        runner: SubprocessRunner,
        logger: RalphLogger,
    ):
        self.ai_runner = ai_runner
        self.runner = runner
        self.logger = logger

    def write_tests(self, task: dict, branch_dir: Path) -> Path:
        """
        Invokes test-writer agent, commits failing tests to branch.
        Returns Path to the committed test file.
        """
        _, _, test_writer = self.ai_runner.assign_agents(task)
        prompt = PromptBuilder.test_writer_prompt(task)
        self.ai_runner.run_test_writer(prompt, branch_dir, agent=test_writer)

        test_file_path = self._discover_test_file(task, branch_dir)

        self.runner.run(
            ["git", "add", str(test_file_path)],
            cwd=branch_dir,
            check=True,
        )
        commit_msg = f"[{task['id']}] {task['title']}: add failing tests"
        self.runner.run(
            ["git", "commit", "-m", commit_msg],
            cwd=branch_dir,
            check=True,
        )

        return test_file_path

    def _discover_test_file(self, task: dict, branch_dir: Path) -> Path:
        """Looks in tests/ directory for files matching test_{task_title}*.py."""
        task_title = task.get("title", "")
        sanitised = re.sub(r"[^a-zA-Z0-9]", "_", task_title.lower())
        pattern = f"test_{sanitised}*.py"

        tests_dir = branch_dir / "tests"
        if not tests_dir.exists():
            raise RalphError(f"tests/ directory not found in {branch_dir}")

        matching = list(tests_dir.glob(pattern))
        if not matching:
            raise RalphError(f"No test file found matching pattern '{pattern}' in tests/ directory")

        if len(matching) > 1:
            self.logger.warn(f"Multiple test files match '{pattern}', using first: {matching[0]}")

        return matching[0]


TestWriter = RalphTestWriter


class TestQualityChecker:
    """
    Two-tier validation of tests written by TestWriter.
    Tier 1: AST-based deterministic checks.
    Tier 2: AI semantic review (only runs if Tier 1 passes).
    """

    def __init__(self, ai_runner: "AIRunner", logger: RalphLogger, config: Config):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def _ast_checks(self, test_source: str, task: dict) -> list[str]:
        """Runs deterministic AST-based checks on test source."""
        issues = []
        try:
            tree = ast.parse(test_source)
        except SyntaxError as e:
            issues.append(f"SyntaxError: {e}")
            return issues

        test_fns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        ]

        if len(test_fns) < len(task.get("acceptance_criteria", [])):
            issues.append(
                f"Fewer tests ({len(test_fns)}) than ACs ({len(task['acceptance_criteria'])})"
            )

        for fn in test_fns:
            pass_or_expr_only = all(isinstance(s, (ast.Pass, ast.Expr)) for s in fn.body)
            if pass_or_expr_only:
                issues.append(f"{fn.name}: empty or pass-only body")

            asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
            if not asserts:
                issues.append(f"{fn.name}: no assertions")
                continue

            for a in asserts:
                if isinstance(a.test, ast.Constant) and a.test.value is True:
                    issues.append(f"{fn.name}: trivially true assertion (assert True)")
                elif isinstance(a.test, ast.Constant) and isinstance(
                    a.test.value, (int, float, str)
                ):
                    issues.append(f"{fn.name}: constant assertion (assert {a.test.value!r})")

        imports = [
            ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        task_title = task.get("title", "")
        module_name = (
            task_title.split("_")[0] if "_" in task_title else task_title.split()[0].lower()
        )
        if not any(module_name.lower() in imp.lower() for imp in imports):
            issues.append("Test file does not appear to import the module under test")

        return issues

    def check(self, task: dict, test_file_path: Path) -> TestQualityResult:
        """Runs two-tier quality check on test file."""
        test_source = test_file_path.read_text(encoding="utf-8")

        deterministic_issues = self._ast_checks(test_source, task)

        if deterministic_issues:
            return TestQualityResult(
                passed=False,
                hollow_tests=[],
                deterministic_issues=deterministic_issues,
                ai_issues=[],
                rounds_used=0,
            )

        ast_report = (
            "\n".join(deterministic_issues) if not deterministic_issues else "Tier 1 passed"
        )
        prompt = PromptBuilder.test_quality_prompt(task, test_source, ast_report)
        ai_output = self.ai_runner.run_reviewer("claude", prompt)

        ai_issues = []
        hollow_tests = []
        for line in ai_output.splitlines():
            match = re.match(r"\[HOLLOW\]\s+(\w+):\s+(.+)", line)
            if match:
                test_name, reason = match.groups()
                hollow_tests.append(test_name)
                ai_issues.append(f"{test_name}: {reason}")

        passed = len(hollow_tests) == 0
        return TestQualityResult(
            passed=passed,
            hollow_tests=hollow_tests,
            deterministic_issues=deterministic_issues,
            ai_issues=ai_issues,
            rounds_used=0,
        )

    def run(
        self, task: dict, test_file_path: Path, test_writer: TestWriter, rounds: int = 0
    ) -> TestQualityResult:
        """Retries test_writer up to max_test_write_rounds if quality fails."""
        max_rounds = self.config.max_test_write_rounds

        while rounds < max_rounds:
            result = self.check(task, test_file_path)

            if result.passed:
                return result

            rounds += 1
            self.logger.warn(f"Test quality check failed (round {rounds}/{max_rounds})")

            if rounds < max_rounds:
                self.logger.info(f"Retrying test writer (round {rounds + 1})...")
                test_writer.write_tests(task, self.config.repo_dir)

        return TestQualityResult(
            passed=False,
            hollow_tests=result.hollow_tests,
            deterministic_issues=result.deterministic_issues,
            ai_issues=result.ai_issues,
            rounds_used=rounds,
        )


class CIPoller:
    """Polls CI completion using commit SHA to avoid stale-data race.

    After every push, captures git rev-parse HEAD to get the commit SHA.
    Uses GitHub REST API via httpx to query runs by head_sha so polling
    is tied to the current commit rather than the branch, avoiding the
    stale 'new run ID' detection problem that caused false timeouts with
    the old branch-based approach.

    On failure: fetches logs via API, invokes coder fix loop, pushes fix,
    captures new SHA, and re-polls. No 'wait for new run' step needed.
    """

    def __init__(
        self,
        runner: SubprocessRunner,
        ai_runner: AIRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.runner = runner
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config
        self._cached_token: str | None = None
        self._cached_repo_slug: str | None = None
        self._http_client: httpx.Client | None = None

    def _get_head_sha(self) -> str:
        result = self.runner.run(["git", "rev-parse", "HEAD"], check=True)
        return result.stdout.strip()

    def _get_gh_token(self) -> str:
        if self._cached_token is None:
            result = self.runner.run(["gh", "auth", "token"], check=True)
            token = result.stdout.strip()
            if not token:
                raise RuntimeError("gh auth token returned empty -- run gh auth login")
            self._cached_token = token
        return self._cached_token

    def _get_repo_slug(self) -> str:
        if self._cached_repo_slug is None:
            result = self.runner.run(["git", "remote", "get-url", "origin"], check=True)
            url = result.stdout.strip()
            if url.startswith("git@github.com:"):
                self._cached_repo_slug = url.removeprefix("git@github.com:").removesuffix(".git")
            elif url.startswith("https://github.com/"):
                self._cached_repo_slug = url.removeprefix("https://github.com/").removesuffix(
                    ".git"
                )
            else:
                raise RuntimeError(f"Cannot parse repo slug from remote URL: {url}")
        return self._cached_repo_slug

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None:
            token = self._get_gh_token()
            self._http_client = httpx.Client(
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
            )
        return self._http_client

    def _gh_api_get(self, path: str) -> dict:
        client = self._get_http_client()
        repo_slug = self._get_repo_slug()
        resp = client.get(
            f"https://api.github.com/repos/{repo_slug}{path}",
        )
        resp.raise_for_status()
        return resp.json()

    def _ci_check_sha(self, head_sha: str) -> dict:
        data = self._gh_api_get(f"/actions/runs?head_sha={head_sha}&per_page=5")
        runs = data.get("workflow_runs", [])

        if not runs:
            return {
                "status": "no_workflow",
                "head_sha": head_sha,
                "run_id": None,
                "run_url": None,
            }

        latest = runs[0]
        wf_status = latest.get("status", "unknown")
        wf_conclusion = latest.get("conclusion")
        run_id = latest.get("id")
        html_url = latest.get("html_url", "")

        if wf_status in (
            "queued",
            "in_progress",
            "waiting",
            "requested",
            "pending",
        ):
            return {
                "status": "running",
                "head_sha": head_sha,
                "run_id": run_id,
                "run_url": html_url,
            }

        if wf_conclusion == "success":
            return {
                "status": "passed",
                "head_sha": head_sha,
                "run_id": run_id,
                "run_url": html_url,
            }

        if wf_conclusion in (
            "failure",
            "error",
            "cancelled",
            "timed_out",
        ):
            return {
                "status": "failed",
                "head_sha": head_sha,
                "run_id": run_id,
                "run_url": html_url,
            }

        return {
            "status": "unknown",
            "head_sha": head_sha,
            "run_id": run_id,
            "run_url": html_url,
        }

    def _ci_wait_sha(self, head_sha: str, timeout: int = 300) -> dict:
        start = time.time()

        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                result = self._ci_check_sha(head_sha)
                result["status"] = "timeout"
                return result

            result = self._ci_check_sha(head_sha)

            if result["status"] in (
                "passed",
                "failed",
                "no_workflow",
                "unknown",
            ):
                return result

            self.logger.info(
                f"CI for SHA {head_sha[:8]}: {result['status']} ({elapsed:.0f}s elapsed)"
            )

            if elapsed < 30:
                interval = 5
            elif elapsed < 120:
                interval = 15
            else:
                interval = 30

            time.sleep(interval)

    def _ci_fetch_failure_logs(self, run_id: int) -> str:
        try:
            client = self._get_http_client()
            repo_slug = self._get_repo_slug()
            resp = client.get(
                f"https://api.github.com/repos/{repo_slug}/actions/runs/{run_id}/logs",
                follow_redirects=True,
            )
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                parts = []
                for name in zf.namelist():
                    with zf.open(name) as f:
                        parts.append(f.read().decode("utf-8", errors="replace"))
            log_text = "\n".join(parts)
            return log_text[-4000:] if len(log_text) > 4000 else log_text
        except Exception:
            result = self.runner.run(
                ["gh", "run", "view", str(run_id), "--log-failed"],
                check=False,
            )
            lines = result.stdout.splitlines()[-150:]
            return "\n".join(lines)

    def wait_for_completion(self, pr_number: int, branch: str) -> CIResult:
        self.logger.info(f"Waiting for CI on PR #{pr_number} (branch: {branch})")

        head_sha = self._get_head_sha()
        self.logger.info(f"Polling CI for SHA: {head_sha[:8]}")

        result = self._ci_wait_sha(head_sha)

        if result["status"] == "passed":
            self.logger.info("CI passed")
            return CIResult(passed=True, rounds_used=1)

        if result["status"] == "no_workflow":
            self.logger.info("No workflow found -- treating as PENDING")
            return CIResult(passed=False, rounds_used=0)

        if result["status"] == "timeout":
            self.logger.warn("CI timed out -- returning retry-able failure")
            return CIResult(passed=False, rounds_used=0)

        self.logger.error(f"CI failed with status: {result['status']}")
        return CIResult(passed=False, rounds_used=1)

    def _check_required_failures(self, pr_number: int) -> tuple[bool, list[str]]:
        try:
            pr_manager = PRManager(self.runner, self.logger)
            checks = pr_manager.get_checks(pr_number)
        except Exception as exc:
            self.logger.warn(f"Could not fetch PR checks (skipping required-check filter): {exc}")
            return False, []

        required_failures = []
        optional_failures = []

        for check in checks:
            conclusion = check.get("conclusion", "")
            if conclusion and conclusion.upper() in ("FAILURE", "ERROR"):
                if check.get("required", True):
                    required_failures.append(check.get("name", "unknown"))
                else:
                    optional_failures.append(check.get("name", "unknown"))

        for name in optional_failures:
            self.logger.warn(f"Optional check failed (ignored): {name}")

        return bool(required_failures), required_failures

    def wait_and_fix(self, task: dict, pr_number: int, branch: str, prd: dict) -> CIResult:
        rounds_used = 0

        while rounds_used < self.config.max_ci_fix_rounds:
            result = self.wait_for_completion(pr_number, branch)

            if result.passed:
                has_required_failure, failing_required = self._check_required_failures(pr_number)
                if not has_required_failure:
                    return CIResult(passed=True, rounds_used=rounds_used)
                self.logger.warn(
                    f"CI run passed but required checks still failing: {failing_required}"
                )

            rounds_used += 1
            self.logger.warn(f"CI failed (round {rounds_used}/{self.config.max_ci_fix_rounds})")

            if rounds_used >= self.config.max_ci_fix_rounds:
                break

            self.logger.info("Fetching CI logs and invoking coder fix loop...")

            head_sha = self._get_head_sha()
            check_result = self._ci_check_sha(head_sha)
            run_id = check_result.get("run_id")

            if run_id:
                failure_log_text = self._ci_fetch_failure_logs(run_id)
            else:
                log_result = self.runner.run(
                    ["gh", "run", "view", "--log-failed"],
                    check=False,
                )
                failure_log = log_result.stdout.splitlines()[-150:]
                failure_log_text = "\n".join(failure_log)

            prompt = PromptBuilder.ci_fix_prompt(task, failure_log_text)
            coder, _, _ = self.ai_runner.assign_agents(task)
            success = self.ai_runner.run_coder(coder, prompt, self.config.repo_dir)

            if not success:
                self.logger.error("Coder fix loop failed")
                raise CIFailedFatal(f"Coder failed on round {rounds_used}")

            self.logger.info("Pushing fix and waiting for CI on new commit...")

            branch_manager = BranchManager(self.config.repo_dir, self.runner, self.logger)
            branch_manager.push_branch(branch)

        self.logger.error(f"CI still failing after {rounds_used} fix rounds")
        raise CIFailedFatal(f"CI still failing after {rounds_used} fix rounds")


class Orchestrator:
    """
    Main orchestrator that composes all components.
    Runs the sprint loop: pre-flight, task selection, execution, cleanup.
    """

    def __init__(self, config: Config, logger: RalphLogger):
        self.config = config
        self.logger = logger
        self.runner = SubprocessRunner(logger)

        self.task_tracker = TaskTracker(
            config.repo_dir / PRD_FILE,
            config.repo_dir / PROGRESS_FILE,
            self.runner,
            logger,
            workstream=config.workstream,
        )
        self.branch_manager = BranchManager(config.repo_dir, self.runner, logger)
        self.pr_manager = PRManager(self.runner, logger)
        self.ai_runner = AIRunner(self.runner, logger, config)
        self.prd_guard = PRDGuard(self.pr_manager, logger)
        self.precommit_gate = PreCommitGate(self.runner, self.ai_runner, logger, config)
        self.test_runner = TestRunner(
            self.runner, self.ai_runner, self.task_tracker, logger, config
        )
        self.test_writer = TestWriter(self.ai_runner, self.runner, logger)
        self.test_quality_checker = TestQualityChecker(self.ai_runner, logger, config)
        self.review_loop = ReviewLoop(self.pr_manager, self.ai_runner, logger, config)
        self.ci_poller = CIPoller(self.runner, self.ai_runner, logger, config)
        self.plan_checker = PlanChecker(self.task_tracker, self.ai_runner, logger)
        self.loop_supervisor = LoopSupervisor(
            logger,
            config.repo_dir / LOG_FILE_NAME,
            config.repo_dir / PROGRESS_FILE,
        )

        self.scrum_master = ScrumMaster(
            self.branch_manager,
            self.pr_manager,
            self.runner,
            logger,
            config.repo_dir,
        )

        self._nested_claude_warning_issued = False

        self._sprint_start_time: datetime | None = None
        self._task_results: list[TaskExecutionResult] = []
        self._iterations_consumed: int = 0

        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        """Kill all active child subprocesses before exiting on SIGTERM."""
        self.logger.warn("[Orchestrator] SIGTERM received — killing active child processes")
        self.runner.kill_active()
        sys.exit(1)

    def _commit_partial_work(self, task: dict, branch: str) -> None:
        """Commit any uncommitted changes the coder left behind before aborting.

        Without this, the next iteration's ensure_main_up_to_date() does
        git reset --hard origin/main and wipes the working tree, losing
        whatever the coder produced before it failed.
        """
        dirty = self.runner.run(["git", "status", "--porcelain"], cwd=self.config.repo_dir)
        if not dirty.stdout.strip():
            self.logger.info("[_commit_partial_work] No uncommitted changes — nothing to preserve")
            return

        self.logger.warn(
            f"[_commit_partial_work] Coder left {len(dirty.stdout.strip().splitlines())} "
            "changed file(s) — committing as partial work before abort"
        )
        self.runner.run(["git", "add", "-A"], cwd=self.config.repo_dir, check=True)
        task_id = task.get("id", "unknown")
        task_title = task.get("title", "untitled")
        self.runner.run(
            [
                "git",
                "commit",
                "--no-verify",
                "-m",
                f"[{task_id}] {task_title} [coder-failed-partial]",
            ],
            cwd=self.config.repo_dir,
            check=True,
        )
        self.logger.info(
            f"[_commit_partial_work] Committed partial work on {branch} — "
            "resume with --resume to continue from this point"
        )

    def _check_cli(self, cmd: str) -> bool:
        """Check if a CLI command is available."""
        try:
            self.runner.run([cmd, "--version"], check=False)
            return True
        except FileNotFoundError:
            return False

    def _preflight(self, prd: dict) -> None:
        """Validates all prerequisites before running tasks."""
        self.logger.info("Running preflight checks...")

        if not self._check_cli("gh"):
            raise PreflightError("gh CLI not found. Install GitHub CLI.")

        if not self._check_cli("git"):
            raise PreflightError("git CLI not found.")

        if "CLAUDECODE" in os.environ and not self._nested_claude_warning_issued:
            self.logger.warn(
                "Running inside Claude Code session — reviewer will fall back to gemini "
                "if claude is unavailable."
            )
            self._nested_claude_warning_issued = True

        try:
            self.branch_manager.verify_ssh_remote()
        except RemoteNotSSHError as e:
            raise PreflightError(f"SSH remote check failed: {e}") from e

        result = self.runner.run(
            ["git", "diff", "--quiet", f"origin/{MAIN_BRANCH}", "--", PRD_FILE],
            cwd=self.config.repo_dir,
        )
        if result.returncode != 0:
            raise PreflightError(
                f"{PRD_FILE} has uncommitted local changes. Commit and push before running ralph."
            )

        try:
            check_result = self.plan_checker.run(prd, ai_check=self.config.validate_plan)
            for warning in check_result.warnings:
                self.logger.warn(f"[AI Plan Check] {warning}")
        except PlanInvalidError as e:
            raise PreflightError(f"Plan validation failed: {e}") from e

        self._kill_stale_opencode_processes()
        self.logger.info("Preflight passed.")

    def _kill_stale_opencode_processes(self) -> None:
        """Kill stale opencode run processes targeting this repo before each iteration."""
        import psutil

        repo_str = str(self.config.repo_dir)
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if (
                    "opencode" in cmdline
                    and "run" in cmdline
                    and any(repo_str in arg for arg in cmdline)
                ):
                    self.logger.warn(f"[Preflight] Killing stale opencode process PID {proc.pid}")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _check_stop_conditions(self, task: dict | None) -> str | None:
        """Check if sprint should stop. Returns reason or None."""
        if task is None:
            return "ALL TASKS COMPLETE"

        if task.get("owner") == "human":
            return "HUMAN_TASK_NEXT"

        return None

    def _run_task_standard(
        self,
        task: dict,
        branch: str,
        prd: dict,
        coder: str,
        reviewer: str,
        pr_info: PRInfo | None,
    ) -> TaskResult:
        """Standard mode state machine.

        Per DESIGN.md Per-Task State Machine (standard mode):
        1. ensure_main_up_to_date() → BranchSyncError → STOP
        2. checkout_or_create(branch) → BranchExistsError → STOP
        3. run_coder() → CoderFailedError → STOP
        4. PreCommitGate.run() → failure after max rounds → WARN, continue
        5. TestRunner.run() → failure after max rounds → WARN, continue
        6. push_branch() → CalledProcessError → STOP
        6.5. PRManager.create() if no existing PR
        7. ReviewLoop.run() → max rounds exceeded → WARN, continue
        8. CIPoller.wait_and_fix() → CIFailedFatal / CITimeoutError → STOP
        9. PRDGuard.check() → PRDGuardViolation → close PR → STOP
        10. PRManager.merge()
        11. BranchManager.merge_and_cleanup()
        12. TaskTracker.mark_complete() → append_progress() → commit_tracking()
        """
        self.logger.info(f"[_run_task_standard] START: {task['id']}")

        # Step 1: ensure_main_up_to_date
        self.logger.info("[_run_task_standard] Step 1: ensure_main_up_to_date")
        try:
            self.branch_manager.ensure_main_up_to_date()
        except BranchSyncError as e:
            self.logger.error(f"[_run_task_standard] Step 1 failed: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=None,
                    ci_passed=False,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="BranchSyncError",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        self.logger.info("[_run_task_standard] Step 1 complete")

        # Step 2: checkout_or_create
        self.logger.info("[_run_task_standard] Step 2: checkout_or_create")
        try:
            branch_status = self.branch_manager.checkout_or_create(branch, self.config.resume)
        except BranchExistsError as e:
            self.logger.error(f"[_run_task_standard] Step 2 failed: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=None,
                    ci_passed=False,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="BranchExistsError",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        self.logger.info("[_run_task_standard] Step 2 complete")

        # Step 3: run_coder
        self.logger.info("[_run_task_standard] Step 3: run_coder")
        try:
            prompt = PromptBuilder.coder_prompt(task, coder, prd, resume=branch_status.had_commits)
            success = self.ai_runner.run_coder(coder, prompt, self.config.repo_dir)
        except Exception as e:
            self.logger.error(f"[_run_task_standard] Step 3 failed: {e}")
            self._commit_partial_work(task, branch)
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=None,
                    ci_passed=False,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="CoderException",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        if not success:
            self.logger.error("[_run_task_standard] Step 3: CoderFailedError")
            self._commit_partial_work(task, branch)
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=None,
                    ci_passed=False,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="CoderFailedError",
                    fatal_error_reason="Coder execution failed",
                )
            )
            return TaskResult(fatal=True, message="Coder execution failed")
        self.logger.info("[_run_task_standard] Step 3 complete")

        # Step 4: PreCommitGate.run()
        self.logger.info("[_run_task_standard] Step 4: PreCommitGate.run")
        precommit_result = self.precommit_gate.run(task, prd, self.config.repo_dir)
        if not precommit_result.passed:
            self.logger.warn(
                f"[_run_task_standard] Step 4: pre-commit failed after "
                f"{precommit_result.rounds_used} rounds"
            )
        else:
            self.logger.info("[_run_task_standard] Step 4 complete")

        # Step 5: TestRunner.run()
        self.logger.info("[_run_task_standard] Step 5: TestRunner.run")
        test_result = self.test_runner.run(task, prd)
        if not test_result.passed:
            self.logger.warn(
                f"[_run_task_standard] Step 5: tests failed after {test_result.rounds_used} rounds"
            )
        else:
            self.logger.info("[_run_task_standard] Step 5 complete")

        # Step 6: push_branch
        self.logger.info("[_run_task_standard] Step 6: push_branch")
        rev_result = self.runner.run(
            ["git", "rev-list", "--count", f"{MAIN_BRANCH}..HEAD"],
            cwd=self.config.repo_dir,
        )
        if rev_result.stdout.strip() == "0":
            dirty = self.runner.run(["git", "status", "--porcelain"], cwd=self.config.repo_dir)
            if dirty.stdout.strip():
                self.logger.warn(
                    "[_run_task_standard] No commits — auto-committing opencode output"
                )
                self.runner.run(["git", "add", "-A"], cwd=self.config.repo_dir, check=True)
                self.runner.run(
                    [
                        "git",
                        "commit",
                        "--no-verify",
                        "-m",
                        f"feat: {task['id']} {task.get('title', '')} [auto-commit]",
                    ],
                    cwd=self.config.repo_dir,
                    check=True,
                )
            else:
                self.logger.error(
                    "[_run_task_standard] Step 6: coder produced no commits and no changes"
                )
                self._task_results.append(
                    TaskExecutionResult(
                        task_id=task["id"],
                        title=task.get("title", "Untitled"),
                        pr_number=None,
                        ci_passed=False,
                        ci_rounds_used=0,
                        escalated=True,
                        fatal_error_type="CoderFailedError",
                        fatal_error_reason="Coder produced no commits and no changes",
                    )
                )
                return TaskResult(fatal=True, message="Coder produced no commits and no changes")
        try:
            self.branch_manager.push_branch(branch)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"[_run_task_standard] Step 6 failed: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=pr_info.number if pr_info else None,
                    ci_passed=False,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="PushFailed",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=f"Push failed: {e}")
        self.logger.info("[_run_task_standard] Step 6 complete")

        # Step 6.5: Create PR if no existing PR
        if pr_info is None:
            self.logger.info("[_run_task_standard] Step 6.5: create PR")
            existing_pr = self.pr_manager.get_existing(branch)
            if existing_pr:
                self.logger.warn(
                    f"[_run_task_standard] Step 6.5: PR #{existing_pr.number} already open"
                    " — reusing"
                )
                pr_info = existing_pr
            else:
                try:
                    pr_info = self.pr_manager.create(
                        branch, task["title"], PromptBuilder.pr_body(task)
                    )
                except subprocess.CalledProcessError as e:
                    self.logger.error(f"[_run_task_standard] Step 6.5 failed: {e}")
                    self._task_results.append(
                        TaskExecutionResult(
                            task_id=task["id"],
                            title=task.get("title", "Untitled"),
                            pr_number=None,
                            ci_passed=False,
                            ci_rounds_used=0,
                            escalated=True,
                            fatal_error_type="PRCreationFailed",
                            fatal_error_reason=str(e),
                        )
                    )
                    return TaskResult(fatal=True, message=f"PR creation failed: {e}")
            self.logger.info("[_run_task_standard] Step 6.5 complete")

        # Step 7: ReviewLoop.run() (only if not skip_review)
        if self.config.skip_review:
            self.logger.info("[_run_task_standard] Step 7: skip_review=true, skipping")
        else:
            self.logger.info("[_run_task_standard] Step 7: ReviewLoop.run")
            review_result = self.review_loop.run(task, pr_info.number, prd, coder, reviewer)
            if review_result.verdict == "CHANGES_REQUESTED_MAX_REACHED":
                self.logger.warn(
                    f"[_run_task_standard] Step 7: max review rounds "
                    f"({review_result.rounds_used}) exceeded"
                )
            elif review_result.verdict == "APPROVED":
                self.logger.info("[_run_task_standard] Step 7 complete")

        # Step 8: CI wait and fix
        self.logger.info("[_run_task_standard] Step 8: CIPoller.wait_and_fix")
        ci_result: CIResult | None = None
        try:
            ci_result = self.ci_poller.wait_and_fix(task, pr_info.number, branch, prd)
        except CIFailedFatal as e:
            self.logger.error("[_run_task_standard] Step 8 failed: CIFailedFatal")
            self.pr_manager.close(pr_info.number, f"CI failed: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=pr_info.number,
                    ci_passed=False,
                    ci_rounds_used=self.config.max_ci_fix_rounds,
                    escalated=True,
                    fatal_error_type="CIFailedFatal",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        except CITimeoutError as e:
            self.logger.error("[_run_task_standard] Step 8 failed: CITimeoutError")
            self.pr_manager.close(pr_info.number, f"CI timeout: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=pr_info.number,
                    ci_passed=False,
                    ci_rounds_used=CI_POLL_MAX_ATTEMPTS,
                    escalated=True,
                    fatal_error_type="CITimeoutError",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        self.logger.info("[_run_task_standard] Step 8 complete")

        # Step 9: PRDGuard.check()
        self.logger.info("[_run_task_standard] Step 9: PRDGuard.check")
        try:
            self.prd_guard.check(pr_info.number)
        except PRDGuardViolation as e:
            self.logger.error("[_run_task_standard] Step 9: PRDGuardViolation")
            self.pr_manager.close(pr_info.number, str(e))
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=pr_info.number,
                    ci_passed=True,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="PRDGuardViolation",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=str(e))
        self.logger.info("[_run_task_standard] Step 9 complete")

        # Step 10: PRManager.merge
        self.logger.info("[_run_task_standard] Step 10: PRManager.merge")
        try:
            self.pr_manager.merge(pr_info.number)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"[_run_task_standard] Step 10 failed: {e}")
            self._task_results.append(
                TaskExecutionResult(
                    task_id=task["id"],
                    title=task.get("title", "Untitled"),
                    pr_number=pr_info.number,
                    ci_passed=True,
                    ci_rounds_used=0,
                    escalated=True,
                    fatal_error_type="MergeFailed",
                    fatal_error_reason=str(e),
                )
            )
            return TaskResult(fatal=True, message=f"Merge failed: {e}")
        self.logger.info("[_run_task_standard] Step 10 complete")

        # Step 11: BranchManager.merge_and_cleanup
        self.logger.info("[_run_task_standard] Step 11: BranchManager.merge_and_cleanup")
        self.branch_manager.merge_and_cleanup(branch)
        self.logger.info("[_run_task_standard] Step 11 complete")

        # Step 12: TaskTracker.mark_complete() → append_progress() → commit_tracking()
        self.logger.info(
            "[_run_task_standard] Step 12: mark_complete → append_progress → commit_tracking"
        )
        now = datetime.now().strftime("%Y-%m-%d")
        sprint_start = (
            self._sprint_start_time.strftime("%Y-%m-%d") if self._sprint_start_time else None
        )
        self.task_tracker.append_progress(
            task["id"], task["title"], pr_info.number, now, sprint_start, self._iterations_consumed
        )
        self.logger.info("progress.txt updated")
        self.task_tracker.mark_complete(task["id"], now, pr_info.number)

        try:
            self.task_tracker.commit_tracking(task["id"], task["title"])
        except subprocess.CalledProcessError as e:
            self.logger.warn(f"Tracking commit failed: {e}")
        self.logger.info("[_run_task_standard] Step 12 complete")

        self.logger.info(f"[_run_task_standard] COMPLETE: {task['id']}")

        self._task_results.append(
            TaskExecutionResult(
                task_id=task["id"],
                title=task.get("title", "Untitled"),
                pr_number=pr_info.number,
                ci_passed=ci_result.passed if ci_result else True,
                ci_rounds_used=ci_result.rounds_used if ci_result else 1,
                escalated=False,
                fatal_error_type=None,
                fatal_error_reason=None,
            )
        )

        return TaskResult(fatal=False)

    def _run_task_tdd(
        self,
        task: dict,
        branch: str,
        prd: dict,
        coder: str,
        reviewer: str,
        pr_info: PRInfo | None,
    ) -> TaskResult:
        """TDD mode: write tests -> quality check -> code -> rest of standard flow."""
        self.logger.info("TDD mode: writing tests...")

        test_file = self.test_writer.write_tests(task, self.config.repo_dir)

        quality_result = self.test_quality_checker.run(task, test_file, self.test_writer)

        if not quality_result.passed:
            msg = (
                f"Test quality failed after "
                f"{self.config.max_test_write_rounds} rounds: "
                f"{quality_result.deterministic_issues + quality_result.ai_issues}"
            )
            return TaskResult(fatal=True, message=msg)

        return self._run_task_standard(task, branch, prd, coder, reviewer, pr_info)

    def _run_task(
        self,
        task: dict,
        branch: str,
        prd: dict,
    ) -> TaskResult:
        """Execute a single task."""
        coder, reviewer, _ = self.ai_runner.assign_agents(task)

        pr_info = None
        if self.config.resume:
            try:
                pr_info = self.pr_manager.get_existing(branch)
                if pr_info:
                    self.logger.info(f"Resuming existing PR #{pr_info.number}")
            except subprocess.CalledProcessError as e:
                return TaskResult(fatal=True, message=f"PR check failed: {e}")

        if self.config.tdd_mode:
            return self._run_task_tdd(task, branch, prd, coder, reviewer, pr_info)

        return self._run_task_standard(task, branch, prd, coder, reviewer, pr_info)

    def run(self, max_iterations: int) -> None:
        """Main loop: preflight, get next task, run until stop or max."""
        self._sprint_start_time = datetime.now()
        self._task_results = []
        self._iterations_consumed = 0

        prd = self.task_tracker.load()

        try:
            self._preflight(prd)
        except PreflightError as e:
            self.logger.fatal(f"Preflight failed: {e}")

        prd = self.task_tracker.load()

        for iteration in range(1, max_iterations + 1):
            self._iterations_consumed = iteration
            task = self.task_tracker.get_next_task()

            stop_reason = self._check_stop_conditions(task)
            if stop_reason:
                self.logger.info(f"Stopping: {stop_reason}")
                self._finalize_run()
                return

            self.logger.info(
                f"Iteration {iteration}/{max_iterations}: {task['id']} {task['title']}"
            )

            branch = f"ralph/{task['id']}-{self.branch_manager.sanitise_branch_name(task['title'])}"

            result = self._run_task(task, branch, prd)

            if result.fatal:
                self.logger.error(f"Task failed: {result.message}")
                self.logger.info("Loop stopped due to fatal error.")
                self._finalize_run()
                return

            prd = self.task_tracker.load()

        self.logger.info(f"Max iterations ({max_iterations}) reached.")
        self._finalize_run()

    def _finalize_run(self) -> None:
        """Handle clean-exit verification and run history recording."""
        self.runner.kill_active()
        self.logger.info("Sprint complete")
        self.logger.info("Loop finished.")
        self.scrum_master._post_sprint_cleanup()

        timestamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
        summary = self._generate_sprint_summary(timestamp)
        summary_filename = f"{SUMMARY_FILE_PREFIX}-{timestamp}.md"
        summary_path = self.config.repo_dir / summary_filename
        summary_path.write_text(summary, encoding="utf-8")
        self.logger.info(f"Sprint summary written to {summary_filename}")

        clean_result = self.loop_supervisor.verify_clean_exit()

        completed_count = sum(
            1 for t in self.task_tracker.load().get("tasks", []) if t.get("completed")
        )

        self.loop_supervisor.record_run(clean_result, completed_count)

    def _generate_sprint_summary(self, timestamp: str) -> str:
        """Generate markdown sprint summary report with YAML frontmatter."""
        sprint_end = datetime.now()
        sprint_start = self._sprint_start_time or sprint_end

        total_tasks = len(self.task_tracker.load().get("tasks", []))
        completed_tasks = [
            t for t in self.task_tracker.load().get("tasks", []) if t.get("completed")
        ]
        tasks_completed_count = len(completed_tasks)

        fatal_errors = [r for r in self._task_results if r.escalated]
        fatal_errors_count = len(fatal_errors)

        if tasks_completed_count > 0:
            completed_without_escalation = tasks_completed_count - fatal_errors_count
            readiness_score = int((completed_without_escalation / tasks_completed_count) * 100)
        else:
            readiness_score = 100

        runtime_seconds = (sprint_end - sprint_start).total_seconds()
        hours = int(runtime_seconds // 3600)
        minutes = int((runtime_seconds % 3600) // 60)
        seconds = int(runtime_seconds % 60)
        runtime_str = f"{hours}h {minutes}m {seconds}s"

        frontmatter = {
            "sprint_start": sprint_start.isoformat(),
            "sprint_end": sprint_end.isoformat(),
            "tasks_completed_count": tasks_completed_count,
            "total_tasks": total_tasks,
            "fatal_errors_count": fatal_errors_count,
            "readiness_score_percent": readiness_score,
        }

        lines = []
        lines.append("---")
        lines.append(yaml.dump(frontmatter, default_flow_style=False, sort_keys=False))
        lines.append("---")
        lines.append("")
        lines.append("# Sprint Summary Report")
        lines.append("")

        lines.append("## Tasks Completed")
        lines.append("")
        lines.append("| ID | Title | PR | Status |")
        lines.append("|---|---|---|---|")
        for task in completed_tasks:
            task_id = task.get("id", "UNKNOWN")
            title = task.get("title", "Untitled")
            pr_num = task.get("pr_number", "")
            status = "merged"
            lines.append(f"| {task_id} | {title} | #{pr_num} | {status} |")
        lines.append("")

        lines.append("## CI Results")
        lines.append("")
        lines.append("| Task ID | Status | Rounds Used |")
        lines.append("|---|---|---|")
        for result in self._task_results:
            status = "PASSED" if result.ci_passed else "FAILED"
            lines.append(f"| {result.task_id} | {status} | {result.ci_rounds_used} |")
        lines.append("")

        if fatal_errors:
            lines.append("## Escalations")
            lines.append("")
            lines.append("| Error Type | Task ID | Reason |")
            lines.append("|---|---|---|")
            for err in fatal_errors:
                error_type = err.fatal_error_type or "unknown"
                reason = err.fatal_error_reason or "No reason provided"
                lines.append(f"| {error_type} | {err.task_id} | {reason} |")
            lines.append("")

        lines.append("## Performance Metrics")
        lines.append("")
        lines.append(f"- **Total Runtime**: {runtime_str}")
        lines.append(f"- **Iterations Consumed**: {self._iterations_consumed}")
        lines.append(f"- **Tasks Completed**: {tasks_completed_count}/{total_tasks}")
        lines.append(f"- **Readiness Score**: {readiness_score}%")
        lines.append("")

        return "\n".join(lines)


@click.group()
def cli():
    """Ralph - AI sprint runner."""
    pass


@cli.command("run")
@click.option(
    "--max",
    "max_iterations",
    default=DEFAULT_MAX_ITERATIONS,
    help="Max iterations (default: 10)",
)
@click.option(
    "--skip-review",
    is_flag=True,
    default=False,
    help="Skip AI review, merge on CI pass",
)
@click.option(
    "--tdd",
    "tdd_mode",
    is_flag=True,
    default=False,
    help="TDD mode: separate test-writer agent writes tests before coder",
)
@click.option(
    "--claude-only",
    is_flag=True,
    default=False,
    help="Use Claude for all agent roles",
)
@click.option(
    "--gemini-only",
    is_flag=True,
    default=False,
    help="Use Gemini for all agent roles",
)
@click.option(
    "--opencode-only",
    is_flag=True,
    default=False,
    help="Use opencode for all agent roles",
)
@click.option(
    "--opencode-model",
    default=DEFAULT_OPENCODE_MODEL,
    show_default=True,
    help="Override opencode coder model",
)
@click.option(
    "--opencode-reviewer-model",
    default=DEFAULT_OPENCODE_REVIEWER_MODEL,
    show_default=True,
    help="Override opencode reviewer model",
)
@click.option(
    "--opencode-test-writer-model",
    default=DEFAULT_OPENCODE_TEST_WRITER_MODEL,
    show_default=True,
    help="Override opencode test-writer model",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Resume stale branches from interrupted runs",
)
@click.option(
    "--max-test-fix-rounds",
    "max_test_fix_rounds",
    default=DEFAULT_MAX_TEST_FIX_ROUNDS,
    help="Max AI fix rounds for test failures (default: 2)",
)
@click.option(
    "--max-test-write-rounds",
    "max_test_write_rounds",
    default=DEFAULT_MAX_TEST_FIX_ROUNDS,
    help="TDD: max rounds to get hollow-free tests (default: 2)",
)
@click.option(
    "--task",
    "force_task_id",
    default=None,
    help="Force a specific task",
)
@click.option(
    "--validate-plan",
    is_flag=True,
    default=False,
    help="AI sanity check on prd.json before sprint (warns, does not block)",
)
@click.option(
    "--no-decompose",
    is_flag=True,
    default=False,
    help="Skip auto-decomposition of complexity-3 tasks",
)
@click.option(
    "--deep-review-check",
    "deep_review_check",
    is_flag=True,
    default=False,
    help="Enable AI meta-review quality check on every review",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print steps without executing AI calls or git ops",
)
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git repo root nearest to cwd)",
)
@click.option(
    "--max-workers",
    "max_workers",
    default=None,
    type=int,
    help="Max parallel tasks in a wave (default: CPU count)",
)
@click.option(
    "--workstream",
    "workstream",
    default=None,
    type=str,
    help="Workstream prefix for worktree branch names (e.g. 'auth' → feature-auth-{task})",
)
def run(
    max_iterations: int,
    skip_review: bool,
    tdd_mode: bool,
    claude_only: bool,
    gemini_only: bool,
    opencode_only: bool,
    opencode_model: str,
    opencode_reviewer_model: str,
    opencode_test_writer_model: str,
    resume: bool,
    max_test_fix_rounds: int,
    max_test_write_rounds: int,
    force_task_id: str | None,
    validate_plan: bool,
    no_decompose: bool,
    deep_review_check: bool,
    dry_run: bool,
    repo_dir: Path | None,
    max_workers: int | None,
    workstream: str | None,
) -> int:
    """Run the AI sprint loop."""
    if repo_dir is None:
        repo_dir = _find_repo_root()

    log_file = repo_dir / LOG_FILE_NAME

    config = Config(
        max_iterations=max_iterations,
        skip_review=skip_review,
        tdd_mode=tdd_mode,
        model_mode="random",
        opencode_model=opencode_model,
        opencode_reviewer_model=opencode_reviewer_model,
        opencode_test_writer_model=opencode_test_writer_model,
        resume=resume,
        repo_dir=repo_dir,
        log_file=log_file,
        max_precommit_rounds=DEFAULT_MAX_PRECOMMIT_ROUNDS,
        max_review_rounds=DEFAULT_MAX_REVIEW_ROUNDS,
        max_ci_fix_rounds=DEFAULT_MAX_CI_FIX_ROUNDS,
        max_test_fix_rounds=max_test_fix_rounds,
        max_test_write_rounds=max_test_write_rounds,
        force_task_id=force_task_id,
        deep_review_check=deep_review_check,
        claude_only=claude_only,
        gemini_only=gemini_only,
        opencode_only=opencode_only,
        validate_plan=validate_plan,
        max_workers=max_workers,
        workstream=workstream,
    )

    logger = RalphLogger(log_file)

    if dry_run:
        logger.info("[DRY-RUN] Starting dry-run mode...")

        prd_path = repo_dir / PRD_FILE
        if not prd_path.exists():
            logger.error(f"[DRY-RUN] No prd.json found at {prd_path}")
            return 1

        with open(prd_path, "r", encoding="utf-8") as f:
            prd = json.load(f)

        tasks = prd.get("tasks", [])
        completed_ids = {t["id"] for t in tasks if t.get("completed")}

        for task in tasks:
            if task.get("completed"):
                continue
            if task.get("owner") == "human":
                continue
            if task.get("decomposed"):
                continue

            depends_on = task.get("depends_on", [])
            if not all(dep_id in completed_ids for dep_id in depends_on):
                continue

            acs = task.get("acceptance_criteria", [])
            estimated_action = "invoke AI coder with task prompt"
            logger.info(f"[DRY-RUN] Would process: {task['id']} {task['title']}")
            logger.info(f"[DRY-RUN]   estimated_action: {estimated_action}")
            logger.info(f"[DRY-RUN]   acceptance_criteria: {len(acs)} criteria")

        logger.info("[DRY-RUN] Dry-run complete.")
        return 0

    orchestrator = Orchestrator(config, logger)
    orchestrator.run(config.max_iterations)

    return 0


@cli.command("init")
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git repo root nearest to cwd)",
)
def init(
    repo_dir: Path | None,
) -> int:
    """Initialize a new ralph project."""
    if repo_dir is None:
        repo_dir = _find_repo_root()

    wizard = DiscoveryWizard(sys.stdin, sys.stdout)
    spec = wizard.run()

    prd_path = repo_dir / PRD_FILE

    prd_content = {
        "project": spec.description,
        "epic_addenda": {},
        "quality_checks": spec.quality_checks,
        "tasks": [
            {
                "id": f"HUMAN-{i + 1:02d}",
                "title": step,
                "description": f"Human-only step: {step}",
                "acceptance_criteria": [f"Complete step: {step}"],
                "owner": "human",
                "completed": False,
            }
            for i, step in enumerate(spec.human_steps)
        ],
    }

    with open(prd_path, "w", encoding="utf-8") as f:
        json.dump(prd_content, f, indent=2)

    coder_instructions_path = repo_dir / "CODER_INSTRUCTIONS.md"
    coder_content = f"""# CODER INSTRUCTIONS

## Project Overview
{spec.description}

## Tech Stack
- Language: {spec.language}
- Runtime: {spec.runtime}
- Package Manager: {spec.package_manager}

## Testing
- Test Framework: {spec.test_framework}
- Coverage Tool: {spec.coverage_tool}

## Quality Gates
"""
    for cmd in spec.quality_checks:
        coder_content += f"- {cmd}\n"
    coder_content += """
## Out of Scope
"""
    for item in spec.out_of_scope:
        coder_content += f"- {item}\n"

    with open(coder_instructions_path, "w", encoding="utf-8") as f:
        f.write(coder_content)

    reviewer_instructions_path = repo_dir / "REVIEWER_INSTRUCTIONS.md"
    reviewer_content = f"""# REVIEWER INSTRUCTIONS

## Review Categories
1. Correctness — logic errors, edge cases, data handling
2. Security — hardcoded secrets, injection, input validation
3. Performance — N+1 queries, unbounded collections
4. Maintainability — functions >50 lines, nesting >4 levels, magic numbers
5. Testing — acceptance criteria from the task are covered; no implementation-testing
6. PRD adherence — implementation matches the task description; nothing out of scope added

## Tech Stack
- Language: {spec.language}
- Test Framework: {spec.test_framework}
- Coverage Tool: {spec.coverage_tool}
"""
    with open(reviewer_instructions_path, "w", encoding="utf-8") as f:
        f.write(reviewer_content)

    hook_path = repo_dir / ".git" / "hooks" / "pre-push"
    hook_content = """#!/bin/bash
# Pre-push hook: block direct commits to main

protected="main"
remote="$1"
url="$2"

while read local_ref local_sha remote_ref remote_sha; do
    if [[ "$local_ref" == "refs/heads/$protected" ]]; then
        echo "ERROR: Direct push to '$protected' is not allowed."
        echo "Please create a branch, commit your changes, and open a PR."
        exit 1
    fi
done

exit 0
"""
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(hook_content)
    hook_path.chmod(0o755)

    print(f"\nInitialized ralph project in {repo_dir}")
    print(f"  - {prd_path}")
    print(f"  - {coder_instructions_path}")
    print(f"  - {reviewer_instructions_path}")
    print(f"  - {hook_path}")

    return 0


def _find_repo_root() -> Path:
    """Walk upward from cwd to find the git repo root.

    Searches for a .git directory starting at cwd and walking up.
    Falls back to cwd if no git root is found.
    """
    start = Path.cwd()
    candidate = start
    while True:
        if (candidate / ".git").exists():
            return candidate.resolve()
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start.resolve()


def _extract_milestone_spec(file_path: Path, milestone: str | None) -> str:
    """Extract spec text from a roadmap file.

    If milestone is given (e.g. 'M4'), finds the section '## Milestone 4'
    and extracts its unticked '- [ ]' deliverables plus the Goal line.
    If milestone is None, finds the first section that has unticked items.
    """
    content = file_path.read_text()
    sections = re.split(r"(?=^## Milestone)", content, flags=re.MULTILINE)

    target = None
    if milestone:
        # Normalise: "M4" → "4", "Milestone 4" → "4"
        num = re.sub(r"[^\d]", "", milestone)
        for section in sections:
            if re.match(rf"## Milestone {num}\b", section):
                target = section
                break
        if target is None:
            raise click.BadParameter(f"Milestone '{milestone}' not found in {file_path}")
    else:
        for section in sections:
            if "- [ ]" in section:
                target = section
                break
        if target is None:
            raise click.UsageError(f"No unticked items found in {file_path}")

    # Pull Goal line and unticked deliverables
    lines = target.splitlines()
    header = lines[0] if lines else ""
    goal_line = next((line for line in lines if line.startswith("**Goal**")), "")
    unticked = [line for line in lines if line.strip().startswith("- [ ]")]

    if not unticked:
        raise click.UsageError(f"No unticked items in {header} — nothing to add")

    return "\n".join([header, goal_line, "", "Deliverables:"] + unticked)


@cli.command("add")
@click.argument("spec")
@click.argument("milestone", required=False, default=None)
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git repo root nearest to cwd)",
)
def add(
    spec: str,
    milestone: str | None,
    repo_dir: Path | None,
) -> int:
    """Add tasks from a natural language spec, roadmap file, or GitHub issue URL.

    SPEC: A natural language description, a path to a roadmap .md file,
    or a GitHub issue URL like https://github.com/user/repo/issues/123

    MILESTONE: Optional milestone name to target in a roadmap file (e.g. M4).
    If omitted when SPEC is a file, uses the first section with unticked items.

    Examples:
      rzilla add roadmap.md M4
      rzilla add roadmap.md
      rzilla add "Build a login page with OAuth"
    """
    if repo_dir is None:
        repo_dir = _find_repo_root()

    # If SPEC is an existing file path, extract the milestone spec from it
    spec_path = Path(spec)
    if spec_path.exists() and spec_path.is_file():
        spec = _extract_milestone_spec(spec_path, milestone)
        print(f"Extracted spec from {spec_path.name}:\n{spec}\n")
    elif milestone:
        raise click.UsageError("MILESTONE argument only valid when SPEC is a roadmap file")

    log_file = repo_dir / LOG_FILE_NAME
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    task_tracker = TaskTracker(
        repo_dir / PRD_FILE,
        repo_dir / PROGRESS_FILE,
        runner,
        logger,
    )
    validator = PrdValidator()

    config = Config(
        max_iterations=1,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model=DEFAULT_OPENCODE_MODEL,
        resume=False,
        repo_dir=repo_dir,
        log_file=log_file,
        max_precommit_rounds=DEFAULT_MAX_PRECOMMIT_ROUNDS,
        max_review_rounds=DEFAULT_MAX_REVIEW_ROUNDS,
        max_ci_fix_rounds=DEFAULT_MAX_CI_FIX_ROUNDS,
        max_test_fix_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        max_test_write_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        force_task_id=None,
    )
    ai_runner = AIRunner(runner, logger, config)

    generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

    tasks = generator.generate(spec)

    print(f"Added {len(tasks)} task(s) to prd.json")
    return 0


@cli.command("plan")
@click.option(
    "--brief",
    "brief",
    default=None,
    help="Plan brief (or reads from stdin if omitted)",
)
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git repo root nearest to cwd)",
)
@click.option(
    "--max-iterations",
    "max_iterations",
    default=3,
    help="Max Planner-Critic iterations (default: 3)",
)
def plan(brief: str | None, repo_dir: Path | None, max_iterations: int) -> int:
    """Generate a work plan from a brief using Planner-Critic loop.

    BRIEF: Optional brief text (or reads from stdin if --brief is omitted).
    """
    if repo_dir is None:
        repo_dir = _find_repo_root()

    if brief is None:
        brief = sys.stdin.read().strip()

    if not brief:
        print("Error: --brief required or stdin must contain brief text", file=sys.stderr)
        return 1

    log_file = repo_dir / LOG_FILE_NAME
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    config = Config(
        max_iterations=1,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model=DEFAULT_OPENCODE_MODEL,
        resume=False,
        repo_dir=repo_dir,
        log_file=log_file,
        max_precommit_rounds=DEFAULT_MAX_PRECOMMIT_ROUNDS,
        max_review_rounds=DEFAULT_MAX_REVIEW_ROUNDS,
        max_ci_fix_rounds=DEFAULT_MAX_CI_FIX_ROUNDS,
        max_test_fix_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        max_test_write_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        force_task_id=None,
    )
    ai_runner = AIRunner(runner, logger, config)
    consensus = PlanConsensus(ai_runner, logger, config)

    consensus.run(brief, max_iterations)

    output_path = repo_dir / PLAN_CONSENSUS_OUTPUT
    print(str(output_path))
    return 0


@cli.command("verify")
@click.argument("task_id")
@click.option(
    "--agent",
    "agent",
    default=None,
    help="AI agent to use for verification (default: gemini)",
)
@click.option(
    "--repo-dir",
    "repo_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git repo root nearest to cwd)",
)
def verify(task_id: str, agent: str | None, repo_dir: Path | None) -> None:
    """Verify acceptance criteria for a task against the implemented code.

    TASK_ID: The task ID to verify (e.g. M6-06).
    """
    if repo_dir is None:
        repo_dir = _find_repo_root()

    log_file = repo_dir / LOG_FILE_NAME
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    config = Config(
        max_iterations=1,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model=DEFAULT_OPENCODE_MODEL,
        resume=False,
        repo_dir=repo_dir,
        log_file=log_file,
        max_precommit_rounds=DEFAULT_MAX_PRECOMMIT_ROUNDS,
        max_review_rounds=DEFAULT_MAX_REVIEW_ROUNDS,
        max_ci_fix_rounds=DEFAULT_MAX_CI_FIX_ROUNDS,
        max_test_fix_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        max_test_write_rounds=DEFAULT_MAX_TEST_FIX_ROUNDS,
        force_task_id=None,
    )

    task_tracker = TaskTracker(repo_dir / PRD_FILE, repo_dir / PROGRESS_FILE, runner, logger)
    ai_runner = AIRunner(runner, logger, config)

    task = task_tracker.get_task_by_id(task_id)
    if task is None:
        print(
            f"Error: Task '{task_id}' not found in prd.json",
            file=sys.stderr,
        )
        sys.exit(1)

    result = _run_verify(task, task_tracker, ai_runner, repo_dir, agent)
    print(result.report)
    sys.exit(result.exit_code)


main = cli  # Backwards compatibility


if __name__ == "__main__":
    sys.exit(main())
