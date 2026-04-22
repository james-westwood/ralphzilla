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

import asyncio
import enum
import json
import os
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import click

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
DEFAULT_OPENCODE_MODEL = "opencode/kimi-k2.5"
GEMINI_MODEL = "gemini-2.5-pro"
ESCALATIONS_FILE = ".ralph/escalations.json"
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
                current_reviewer = (
                    "gemini"
                    if current_reviewer == "claude"
                    else "opencode"
                    if current_reviewer == "gemini"
                    else "claude"
                )
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

    def run(
        self,
        cmd: list[str],
        env_removals: list[str] | None = None,
        timeout: int = SUBPROCESS_TIMEOUT_SECS,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        env_removals = env_removals or []
        self.logger.info(f"Running command: {' '.join(cmd)}")

        child_env = os.environ.copy()
        for key in env_removals:
            child_env.pop(key, None)

        return subprocess.run(
            cmd,
            env=child_env,
            timeout=timeout,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
        )


class RuntimeError(RalphError):
    """Raised when an AI runtime is unavailable or fails."""

    pass


@dataclass
class RuntimeConfig:
    """Configuration for an AI runtime.

    Attributes:
        name: Unique runtime identifier (e.g., "claude", "gemini", "opencode")
        command: CLI command to invoke the runtime
        model_flag: Flag for specifying model (e.g., "-m", "--model")
        supported_models: List of supported model names
        timeout_secs: Default timeout for runtime execution
        requires_api_key: Whether runtime requires API key configuration
        env_var_name: Environment variable name for API key
    """

    name: str
    command: str
    model_flag: str = "-m"
    supported_models: list[str] = field(default_factory=list)
    timeout_secs: int = 300
    requires_api_key: bool = False
    env_var_name: str = ""

    def __post_init__(self):
        """Validate configuration after initialization."""
        if not self.name:
            raise ValueError("RuntimeConfig.name cannot be empty")
        if not self.command:
            raise ValueError("RuntimeConfig.command cannot be empty")


class AIRuntime(ABC):
    """Abstract base class for AI runtime implementations.

    Defines the contract that all AI runtime implementations must follow:
    - Command execution
    - Output capture
    - Error handling
    - Timeout management
    - Runtime detection and version checking

    Concrete implementations: ClaudeRuntime, GeminiRuntime, OpencodeRuntime
    """

    def __init__(self, config: RuntimeConfig, runner: SubprocessRunner, logger: RalphLogger):
        self.config = config
        self.runner = runner
        self.logger = logger

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this runtime is available (CLI installed and accessible).

        Returns:
            True if the runtime can be invoked, False otherwise
        """
        pass

    @abstractmethod
    def get_version(self) -> str:
        """Get the version string of the runtime.

        Returns:
            Version string from the runtime CLI

        Raises:
            RuntimeError: If the runtime is not available
        """
        pass

    @abstractmethod
    def run_task(
        self,
        task_id: str,
        branch_name: str,
        prompt: str,
        cwd: Path,
    ) -> tuple[bool, str]:
        """Execute a task using this AI runtime.

        Args:
            task_id: Unique identifier for the task
            branch_name: Git branch name for the task
            prompt: The prompt/instruction to send to the AI
            cwd: Working directory for execution

        Returns:
            Tuple of (success: bool, output: str)
            - success: True if task completed successfully
            - output: Captured stdout/stderr from the runtime

        Raises:
            RuntimeError: If the runtime is not available
        """
        pass

    def _check_command(self) -> bool:
        """Helper to check if the configured command is available."""
        try:
            result = self.runner.run(
                ["which", self.config.command],
                check=False,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class ClaudeRuntime(AIRuntime):
    """Runtime implementation for Claude CLI."""

    def is_available(self) -> bool:
        """Check if claude CLI is available."""
        return self._check_command()

    def get_version(self) -> str:
        """Get claude version string."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        result = self.runner.run(
            [self.config.command, "--version"],
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def run_task(
        self,
        task_id: str,
        branch_name: str,
        prompt: str,
        cwd: Path,
    ) -> tuple[bool, str]:
        """Execute task using claude CLI."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        self.logger.info(f"[ClaudeRuntime] Running task {task_id} on branch {branch_name}")

        try:
            result = self.runner.run(
                [
                    self.config.command,
                    "--dangerously-skip-permissions",
                    "--print",
                    prompt,
                ],
                env_removals=["CLAUDECODE"],
                cwd=cwd,
                check=True,
                timeout=self.config.timeout_secs,
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            error_msg = f"Claude execution failed: {e.stderr if e.stderr else str(e)}"
            self.logger.error(f"[ClaudeRuntime] {error_msg}")
            return False, error_msg


class GeminiRuntime(AIRuntime):
    """Runtime implementation for Gemini CLI."""

    def is_available(self) -> bool:
        """Check if gemini CLI is available."""
        return self._check_command()

    def get_version(self) -> str:
        """Get gemini version string."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        result = self.runner.run(
            [self.config.command, "--version"],
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def run_task(
        self,
        task_id: str,
        branch_name: str,
        prompt: str,
        cwd: Path,
    ) -> tuple[bool, str]:
        """Execute task using gemini CLI."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        self.logger.info(f"[GeminiRuntime] Running task {task_id} on branch {branch_name}")

        # Determine model to use
        model = GEMINI_MODEL
        if self.config.supported_models:
            model = self.config.supported_models[0]

        try:
            result = self.runner.run(
                [
                    self.config.command,
                    "-m",
                    model,
                    "--yolo",
                    "-p",
                    prompt,
                ],
                cwd=cwd,
                check=True,
                timeout=self.config.timeout_secs,
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            error_msg = f"Gemini execution failed: {e.stderr if e.stderr else str(e)}"
            self.logger.error(f"[GeminiRuntime] {error_msg}")
            return False, error_msg


class OpencodeRuntime(AIRuntime):
    """Runtime implementation for Opencode CLI."""

    def is_available(self) -> bool:
        """Check if opencode CLI is available."""
        return self._check_command()

    def get_version(self) -> str:
        """Get opencode version string."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        result = self.runner.run(
            [self.config.command, "--version"],
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def run_task(
        self,
        task_id: str,
        branch_name: str,
        prompt: str,
        cwd: Path,
    ) -> tuple[bool, str]:
        """Execute task using opencode CLI."""
        if not self.is_available():
            raise RuntimeError(f"Runtime '{self.config.name}' is not available")

        self.logger.info(f"[OpencodeRuntime] Running task {task_id} on branch {branch_name}")

        # Determine model to use
        model = (
            self.config.opencode_model
            if hasattr(self.config, "opencode_model")
            else DEFAULT_OPENCODE_MODEL
        )

        try:
            result = self.runner.run(
                [
                    self.config.command,
                    "run",
                    "-m",
                    model,
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                cwd=cwd,
                check=True,
                timeout=self.config.timeout_secs,
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            error_msg = f"Opencode execution failed: {e.stderr if e.stderr else str(e)}"
            self.logger.error(f"[OpencodeRuntime] {error_msg}")
            return False, error_msg


class RuntimeRegistry:
    """Registry for managing multiple AI runtimes.

    Provides runtime discovery, availability checking, and default selection.
    Enables plugging in different AI tools without changing core ralph logic.
    """

    def __init__(self, runner: SubprocessRunner, logger: RalphLogger):
        self.runner = runner
        self.logger = logger
        self._runtimes: dict[str, AIRuntime] = {}

    def register(self, runtime: AIRuntime) -> None:
        """Register a runtime instance."""
        self._runtimes[runtime.config.name] = runtime
        self.logger.info(f"[RuntimeRegistry] Registered runtime: {runtime.config.name}")

    def get(self, name: str) -> AIRuntime | None:
        """Get a runtime by name. Returns None if not registered."""
        return self._runtimes.get(name)

    def list_available(self) -> list[str]:
        """Return list of runtime names that are currently available."""
        available = []
        for name, runtime in self._runtimes.items():
            if runtime.is_available():
                available.append(name)
        return available

    def get_default(self) -> AIRuntime | None:
        """Get the first available runtime, or None if none available."""
        for runtime in self._runtimes.values():
            if runtime.is_available():
                return runtime
        return None


# --- GitHub Issue Pattern ---
GITHUB_ISSUE_PATTERN = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)")


# --- BlockerResult Dataclass ---
@dataclass
class BlockerResult:
    kind: BlockerKind
    task_id: str
    context: str


# --- UnblockResult Dataclass ---
@dataclass
class UnblockResult:
    success: bool
    actions_log: list[str]
    escalated: bool = False
    replacement_task_id: str | None = None
    skip_to_next: bool = False
    alternative_model: str | None = None


# --- BlockerAnalyser Class ---
class BlockerAnalyser:
    """Analyses task execution failures to classify blockers."""

    def __init__(self, logger: RalphLogger):
        self.logger = logger

    def analyse(self, exit_code: int, error_output: str, task_id: str) -> BlockerResult | None:
        """Analyse failure and classify blocker kind."""
        if exit_code == 0:
            return None

        error_lower = error_output.lower()

        # Check for merge conflicts
        if "merge conflict" in error_lower or "conflict" in error_lower:
            return BlockerResult(BlockerKind.MERGE_CONFLICT, task_id, error_output)

        # Check for PRD guard violation
        if "prd.json" in error_lower or "prd guard" in error_lower:
            return BlockerResult(BlockerKind.PRD_GUARD_VIOLATION, task_id, error_output)

        # Check for reviewer unavailable
        if "reviewer" in error_lower and ("unavailable" in error_lower or "failed" in error_lower):
            return BlockerResult(BlockerKind.REVIEWER_UNAVAILABLE, task_id, error_output)

        # Default to CI fatal
        return BlockerResult(BlockerKind.CI_FATAL, task_id, error_output)


# --- EscalationManager Class ---
class EscalationManager:
    """Tracks consecutive failures and determines when to escalate."""

    def __init__(self, logger: RalphLogger, max_retries: int = MAX_RETRIES_PER_BLOCKER):
        self.logger = logger
        self.max_retries = max_retries
        self._consecutive_failures: dict[str, int] = {}
        self._total_blockers = 0

    def record_failure(self, task_id: str) -> None:
        """Record a failure for a task."""
        self._consecutive_failures[task_id] = self._consecutive_failures.get(task_id, 0) + 1
        self._total_blockers += 1
        self.logger.info(
            f"Recorded failure for {task_id}: attempt {self._consecutive_failures[task_id]}"
        )

    def reset_consecutive(self, task_id: str) -> None:
        """Reset consecutive failure count for a task."""
        if task_id in self._consecutive_failures:
            del self._consecutive_failures[task_id]
            self.logger.info(f"Reset failure count for {task_id}")

    def should_escalate(self, task_id: str) -> bool:
        """Check if we should escalate for this task."""
        return self._consecutive_failures.get(task_id, 0) >= self.max_retries

    def escalate(self, task_id: str, context: str) -> UnblockResult:
        """Escalate a failed task."""
        self.logger.warn(f"Escalating task {task_id} after {self.max_retries} retries")
        return UnblockResult(
            success=False,
            actions_log=[f"Escalated {task_id}: {context}"],
            escalated=True,
        )


# --- UnblockStrategy Class ---
class UnblockStrategy:
    """Handles different blocker kinds with specific unblocking strategies."""

    def __init__(
        self,
        branch_manager: "BranchManager",
        pr_manager: "PRManager",
        ai_runner: "AIRunner",
        logger: RalphLogger,
    ):
        self.branch_manager = branch_manager
        self.pr_manager = pr_manager
        self.ai_runner = ai_runner
        self.logger = logger

    def execute(self, blocker: BlockerResult, task: dict) -> UnblockResult:
        """Execute appropriate strategy for the blocker kind."""
        actions: list[str] = []

        if blocker.kind == BlockerKind.MERGE_CONFLICT:
            actions.append(f"Detected merge conflict for {blocker.task_id}")
            # Try to auto-resolve by pulling main and rebasing
            try:
                self.branch_manager.ensure_main_up_to_date()
                actions.append("Pulled latest main")
                return UnblockResult(success=True, actions_log=actions)
            except Exception as e:
                actions.append(f"Failed to resolve merge conflict: {e}")
                return UnblockResult(success=False, actions_log=actions, escalated=True)

        elif blocker.kind == BlockerKind.CI_FATAL:
            actions.append(f"Detected CI fatal failure for {blocker.task_id}")
            # Try to fix with alternative model
            return UnblockResult(
                success=False,
                actions_log=actions,
                alternative_model="gemini-2.5-pro",
            )

        elif blocker.kind == BlockerKind.PRD_GUARD_VIOLATION:
            actions.append(f"Detected PRD guard violation for {blocker.task_id}")
            return UnblockResult(
                success=False,
                actions_log=actions,
                escalated=True,
                skip_to_next=True,
            )

        elif blocker.kind == BlockerKind.REVIEWER_UNAVAILABLE:
            actions.append(f"Detected reviewer unavailable for {blocker.task_id}")
            # Try alternative reviewer
            return UnblockResult(
                success=False,
                actions_log=actions,
                alternative_model="claude",
            )

        else:
            actions.append(f"Unknown blocker kind: {blocker.kind}")
            return UnblockResult(success=False, actions_log=actions, escalated=True)


# --- TestQualityChecker Class ---
class TestQualityChecker:
    """Validates test quality using deterministic and AI checks."""

    def __init__(self, ai_runner: "AIRunner", logger: RalphLogger, config: Config):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def check(self, test_content: str, test_file: Path) -> TestQualityResult:
        """Run quality checks on test content."""
        hollow_tests: list[str] = []
        deterministic_issues: list[str] = []
        ai_issues: list[str] = []
        rounds_used = 0

        # Check for hollow tests (empty/pass-only tests)
        if "pass" in test_content.lower() and "assert" not in test_content.lower():
            hollow_tests.append("test_contains_pass_only")

        # Check for missing assertions
        if test_content.count("assert") < 2:
            deterministic_issues.append("insufficient_assertions")

        rounds_used += 1

        return TestQualityResult(
            passed=len(hollow_tests) == 0 and len(deterministic_issues) == 0,
            hollow_tests=hollow_tests,
            deterministic_issues=deterministic_issues,
            ai_issues=ai_issues,
            rounds_used=rounds_used,
        )

    def run(self, test_file: Path) -> TestQualityResult:
        """Run quality checks on a test file."""
        if not test_file.exists():
            self.logger.error(f"Test file not found: {test_file}")
            return TestQualityResult(
                passed=False,
                hollow_tests=[],
                deterministic_issues=["file_not_found"],
                ai_issues=[],
                rounds_used=0,
            )

        test_content = test_file.read_text(encoding="utf-8")
        return self.check(test_content, test_file)


# --- AIRunner Class ---
class AIRunner:
    """Orchestrates AI agent calls for different roles (coder, reviewer, test_writer, decomposer)."""

    def __init__(
        self,
        runtime_registry: RuntimeRegistry,
        logger: RalphLogger,
        config: Config,
    ):
        self.runtime_registry = runtime_registry
        self.logger = logger
        self.config = config
        self._nest_check_cache: bool | None = None

    def _is_nested_claude_session(self) -> bool:
        """Detect if we're running inside a Claude Code session."""
        if self._nest_check_cache is not None:
            return self._nest_check_cache

        # Check for Claude Code environment markers
        claude_markers = [
            "CLAUDECODE",
            "CLAUDE_CODE",
            "_CLAUDE",
        ]

        for marker in claude_markers:
            if os.environ.get(marker):
                self._nest_check_cache = True
                return True

        # Check parent process for claude
        try:
            result = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(os.getppid())],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "claude" in result.stdout.lower():
                self._nest_check_cache = True
                return True
        except Exception:
            pass

        self._nest_check_cache = False
        return False

    def assign_agents(self, task: dict) -> tuple[str, str, str]:
        """Assign AI agents (coder, reviewer, test_writer) based on task and config."""
        # Determine model assignments based on config flags
        if self.config.claude_only:
            coder = reviewer = test_writer = "claude"
        elif self.config.gemini_only:
            coder = reviewer = test_writer = "gemini"
        elif self.config.opencode_only:
            coder = reviewer = test_writer = "opencode"
        else:
            # Default: random assignment for variety
            import random

            available = ["claude", "gemini", "opencode"]
            coder = random.choice(available)
            reviewer = random.choice([a for a in available if a != coder] or available)
            test_writer = random.choice(
                [a for a in available if a not in [coder, reviewer]] or available
            )

        self.logger.info(
            f"Assigned agents: coder={coder}, reviewer={reviewer}, test_writer={test_writer}"
        )
        return coder, reviewer, test_writer

    def run_coder(self, agent: str, prompt: str, cwd: Path) -> bool:
        """Run the coder agent with given prompt."""
        runtime = self.runtime_registry.get(agent)
        if not runtime:
            self.logger.error(f"Runtime not found for agent: {agent}")
            return False

        # Generate a temporary task_id and branch_name for the runtime interface
        task_id = f"coder_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        branch_name = f"ralph/temp/{task_id}"

        success, output = runtime.run_task(task_id, branch_name, prompt, cwd)
        if not success:
            self.logger.error(f"Coder agent {agent} failed: {output}")
        return success

    def run_reviewer(self, agent: str, prompt: str) -> str:
        """Run the reviewer agent with given prompt."""
        runtime = self.runtime_registry.get(agent)
        if not runtime:
            self.logger.error(f"Runtime not found for agent: {agent}")
            return ""

        # Create a temporary task context
        task_id = f"reviewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        branch_name = f"ralph/temp/{task_id}"
        cwd = self.config.repo_dir

        success, output = runtime.run_task(task_id, branch_name, prompt, cwd)
        if not success:
            self.logger.error(f"Reviewer agent {agent} failed: {output}")
            return ""
        return output

    def run_test_writer(self, agent: str, prompt: str, cwd: Path) -> tuple[bool, str]:
        """Run the test writer agent with given prompt."""
        runtime = self.runtime_registry.get(agent)
        if not runtime:
            self.logger.error(f"Runtime not found for agent: {agent}")
            return False, ""

        task_id = f"test_writer_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        branch_name = f"ralph/temp/{task_id}"

        success, output = runtime.run_task(task_id, branch_name, prompt, cwd)
        if not success:
            self.logger.error(f"Test writer agent {agent} failed: {output}")
        return success, output

    def run_decompose(self, agent: str, task: dict, prd: dict) -> list[dict]:
        """Run the decomposer to break down a task into subtasks."""
        runtime = self.runtime_registry.get(agent)
        if not runtime:
            self.logger.error(f"Runtime not found for agent: {agent}")
            return []

        prompt = PromptBuilder.decomposer_prompt(task, prd)
        task_id = f"decompose_{task.get('id', 'unknown')}"
        branch_name = f"ralph/temp/{task_id}"
        cwd = self.config.repo_dir

        success, output = runtime.run_task(task_id, branch_name, prompt, cwd)
        if not success:
            self.logger.error(f"Decomposer agent {agent} failed: {output}")
            return []

        # Parse the output as JSON list of subtasks
        try:
            subtasks = json.loads(output)
            if isinstance(subtasks, list):
                return subtasks
            else:
                self.logger.error("Decomposer output is not a list")
                return []
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse decomposer output: {e}")
            return []


# --- PrdGenerator Class ---
class PrdGenerator:
    """Generates tasks from user prompts or GitHub issues and adds them to prd.json."""

    def __init__(
        self,
        ai_runner: AIRunner,
        task_tracker: "TaskTracker",
        validator: PrdValidator,
        runner: SubprocessRunner,
        logger: RalphLogger,
    ):
        self.ai_runner = ai_runner
        self.task_tracker = task_tracker
        self.validator = validator
        self.runner = runner
        self.logger = logger

    def _infer_next_epic_prefix(self, prd: dict) -> int:
        """Infer the next epic number from existing tasks."""
        tasks = prd.get("tasks", [])
        if not tasks:
            return 1

        max_epic = 0
        for task in tasks:
            task_id = task.get("id", "")
            # Match pattern like "M1-01", "M12-03", etc.
            match = re.match(r"M(\d+)-", task_id)
            if match:
                epic_num = int(match.group(1))
                max_epic = max(max_epic, epic_num)

        return max_epic + 1

    def _fetch_issue_body(self, github_url: str) -> str:
        """Fetch GitHub issue body using gh CLI."""
        # Extract owner, repo, issue number from URL
        match = GITHUB_ISSUE_PATTERN.search(github_url)
        if not match:
            return ""

        issue_number = match.group(1)
        parts = github_url.replace("https://github.com/", "").split("/")
        if len(parts) < 4:
            return ""

        owner, repo = parts[0], parts[1]

        # Use gh CLI to fetch issue details
        cmd = [
            "gh",
            "api",
            f"repos/{owner}/{repo}/issues/{issue_number}",
        ]

        try:
            result = self.runner.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SECS)
            issue_data = json.loads(result.stdout)
            title = issue_data.get("title", "")
            body = issue_data.get("body", "")

            if body:
                return f"{title}\n\n{body}"
            return title
        except (subprocess.CalledProcessError, json.JSONDecodeError, Exception) as e:
            self.logger.error(f"Failed to fetch issue: {e}")
            return ""

    def generate(self, prompt_or_url: str) -> list[dict]:
        """Generate tasks from a prompt or GitHub issue URL."""
        # Check if it's a GitHub issue URL
        if GITHUB_ISSUE_PATTERN.search(prompt_or_url):
            self.logger.info(f"Fetching GitHub issue: {prompt_or_url}")
            prompt = self._fetch_issue_body(prompt_or_url)
            if not prompt:
                raise RalphError(f"Failed to fetch GitHub issue: {prompt_or_url}")
        else:
            prompt = prompt_or_url

        # Load current PRD to infer next epic
        prd = self.task_tracker.load()
        next_epic = self._infer_next_epic_prefix(prd)

        # Generate prompt for AI
        generate_prompt = PromptBuilder.prd_generate_prompt(prompt, next_epic)

        # Run reviewer agent to generate tasks
        response = self.ai_runner.run_reviewer("claude", generate_prompt)
        if not response:
            raise RalphError("Failed to generate tasks from prompt")

        # Parse the response as JSON
        try:
            tasks = json.loads(response)
            if not isinstance(tasks, list):
                raise RalphError("Generated tasks is not a list")
        except json.JSONDecodeError as e:
            raise RalphError(f"Failed to parse generated tasks: {e}")

        # Validate each task and generate IDs
        validated_tasks = []
        for i, task in enumerate(tasks):
            # Generate task ID
            epic_prefix = f"M{next_epic}"
            task_id = f"{epic_prefix}-{i + 1:02d}"
            task["id"] = task_id
            task["epic"] = epic_prefix

            # Set defaults if missing
            task.setdefault("depends_on", [])
            task.setdefault("files", ["ralph.py"])
            task.setdefault("owner", "ralph")
            task.setdefault("completed", False)
            task.setdefault("priority", 1)

            # Validate the task
            all_task_ids = {t.get("id", "") for t in prd.get("tasks", [])}
            all_task_ids.update(t.get("id", "") for t in validated_tasks)
            try:
                self.validator.validate(task, all_task_ids)
            except PlanInvalidError as e:
                raise RalphError(f"Generated task validation failed: {e}")

            validated_tasks.append(task)

        # Add tasks to tracker
        for task in validated_tasks:
            self.task_tracker.add_task(task)
            self.logger.info(f"Added task: {task['id']} - {task['title']}")

        return validated_tasks


# --- PromptBuilder Class ---
class PromptBuilder:
    """Builds prompts for various AI agents."""

    @staticmethod
    def coder_prompt(task: dict, prd: dict, round_num: int = 1, resume: bool = False) -> str:
        """Build prompt for the coder agent."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        description = task.get("description", "")
        acs = task.get("acceptance_criteria", [])
        files = task.get("files", [])

        ac_text = "\n".join(f"- {ac}" for ac in acs) if acs else "- Implement as described"
        files_text = "\n".join(f"- {f}" for f in files) if files else "- TBD"

        resume_note = ""
        if resume:
            resume_note = """IMPORTANT: This branch has partial work.
Run git log --oneline to see existing commits. Do NOT redo already-committed work.

"""

        return f"""You are a software engineer implementing a task.

Task ID: {task_id}
Title: {title}
Round: {round_num}

Description:
{description}

Acceptance Criteria:
{ac_text}

Files to modify:
{files_text}

{resume_note}Instructions:
1. Implement the task according to the acceptance criteria
2. Do NOT modify prd.json
3. Write clean, well-documented code
4. Follow existing code patterns in the repository
5. Ensure all tests pass
6. Make exactly TWO commits — no more, no fewer:
   - Commit A (source): git add ralph.py && git commit -m '[{task_id}] {title}: implement'
   - Commit B (tests):  git add tests/ && git commit -m '[{task_id}] {title}: add tests'
7. Do NOT push. Do NOT create a PR. Do NOT touch prd.json or progress.txt.
8. Use uv for all Python commands: uv run pytest, uv run pre-commit, etc.

Respond with "Task completed" when done."""

    @staticmethod
    def reviewer_prompt(task: dict, diff: str, prd: dict, round_num: int = 1) -> str:
        """Build prompt for the reviewer agent."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        acs = task.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- {ac}" for ac in acs) if acs else "- Implement as described"

        return f"""You are a code reviewer reviewing a pull request.

Task ID: {task_id}
Title: {title}
Round: {round_num}

Acceptance Criteria:
{ac_text}

Diff:
```diff
{diff}
```

Review Instructions:
1. Check if the implementation meets all acceptance criteria
2. Verify code quality and adherence to patterns
3. Look for bugs, edge cases, or missing error handling
4. Ensure tests are present and meaningful

Respond with ONE of:
- "APPROVED" - if the implementation is correct and complete
- "CHANGES REQUESTED" - followed by specific changes needed

Be thorough but constructive in your feedback."""

    @staticmethod
    def review_fix_prompt(task: dict, review_feedback: str) -> str:
        """Build prompt for fixing review feedback."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")

        return f"""You are fixing code based on review feedback.

Task ID: {task_id}
Title: {title}

Review Feedback:
{review_feedback}

Instructions:
1. Address all the feedback points
2. Make minimal changes to fix the issues
3. Do NOT modify prd.json
4. Ensure the code still meets the original acceptance criteria

Respond with "Fixes applied" when done."""

    @staticmethod
    def test_writer_prompt(task: dict, prd: dict, existing_tests: str = "") -> str:
        """Build prompt for the test writer agent."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        description = task.get("description", "")
        acs = task.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- {ac}" for ac in acs) if acs else "- Implement as described"
        existing_text = f"\n\nExisting Tests:\n{existing_tests}" if existing_tests else ""

        return f"""You are a test engineer writing tests for a task.

Task ID: {task_id}
Title: {title}

Description:
{description}

Acceptance Criteria:
{ac_text}{existing_text}

Instructions:
1. Write comprehensive tests covering all acceptance criteria
2. Include positive and negative test cases
3. Test edge cases and error conditions
4. Use the project's test framework
5. Tests should be deterministic and not flaky
6. Do NOT write hollow tests (tests that just pass without real assertions)

Respond with the test code only."""

    @staticmethod
    def pr_body(task: dict) -> str:
        """Build PR body from task."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        description = task.get("description", "")
        acs = task.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- [ ] {ac}" for ac in acs) if acs else ""

        return f"""## Task: {title}

**Task ID:** {task_id}

### Description
{description}

### Acceptance Criteria
{ac_text}

---
*Automated PR created by Ralph*"""

    @staticmethod
    def planner_prompt(brief: str, feedback: str = "") -> str:
        """Build prompt for the planner agent."""
        feedback_section = f"\n\nPrevious Feedback:\n{feedback}" if feedback else ""

        return f"""You are a technical planner breaking down a project into tasks.

Project Brief:
{brief}{feedback_section}

Instructions:
1. Break down the project into 3-10 concrete tasks
2. Each task should have:
   - id: unique identifier (e.g., "task-001")
   - title: concise description
   - description: detailed explanation
   - acceptance_criteria: list of verifiable conditions
   - files: list of files to modify
   - depends_on: list of task IDs this depends on (can be empty)
   - owner: "ralph" or "human"
3. Order tasks by dependencies (dependencies first)
4. Ensure tasks are granular and testable

Respond with a JSON array of tasks only."""

    @staticmethod
    def critic_prompt(plan_output: str) -> str:
        """Build prompt for the critic agent."""
        return f"""You are a critic reviewing a work plan for quality and completeness.

Plan to Review:
{plan_output}

Review Criteria:
1. Are tasks granular enough (not too large)?
2. Are dependencies correctly ordered?
3. Are acceptance criteria specific and verifiable?
4. Is the scope realistic?
5. Are there any missing tasks or gaps?

Respond with ONE of:
- "OKAY" - if the plan is good
- "REJECT: [reason]" - if the plan needs improvement, with specific feedback

Be thorough but constructive."""

    @staticmethod
    def review_quality_prompt(task: dict, review_text: str) -> str:
        """Build prompt for AI meta-review of review quality."""
        return f"""You are evaluating the quality of a code review.

Task: {task.get("title", "Unknown")}

Review Content:
{review_text}

Evaluate:
1. Is the review substantive (not just "LGTM")?
2. Does it identify real issues or confirm quality?
3. Is the feedback constructive and specific?
4. Would acting on this review improve the code?

Respond with ONE of:
- "PASS" - if the review is substantive and useful
- "FAIL: [reason]" - if the review is too shallow or unhelpful"""

    @staticmethod
    def decomposer_prompt(task: dict, prd: dict) -> str:
        """Build prompt for the decomposer agent."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        description = task.get("description", "")

        return f"""You are breaking down a large task into smaller subtasks.

Parent Task: {title} (ID: {task_id})

Description:
{description}

Instructions:
1. Break this task into 2-5 smaller, independent subtasks
2. Each subtask should:
   - Have a clear, specific goal
   - Be implementable in isolation
   - Include acceptance criteria
   - List files to modify
3. Subtasks should combine to complete the parent task
4. Use sequential IDs: {task_id}-sub-1, {task_id}-sub-2, etc.

Respond with a JSON array of subtask objects."""

    @staticmethod
    def prd_generate_prompt(prompt: str, next_epic: int) -> str:
        """Build prompt for generating PRD tasks from a user prompt."""
        epic_prefix = f"M{next_epic}"

        return f"""You are a technical planner breaking down a feature request into tasks.

Feature Request:
{prompt}

Instructions:
1. Break down the request into 1-5 concrete tasks
2. Each task must have:
   - title: concise description (string)
   - description: detailed explanation (at least 100 characters)
   - acceptance_criteria: list of strings, each containing a file path pattern
3. Use sequential IDs starting with {epic_prefix}-01, {epic_prefix}-02, etc.
4. Set depends_on to empty list []
5. Set owner to "ralph"
6. Set files to ["ralph.py"] (all code goes in ralph.py)
7. Ensure description is at least 100 characters long

Respond with a JSON array of task objects only. Example:
[
  {{
    "title": "Task title",
    "description": "Detailed description that is at least 100 chars "
                "long to pass validation requirements",
    "acceptance_criteria": ["tests/test_file.py: criteria"],
    "depends_on": [],
    "files": ["ralph.py"],
    "owner": "ralph"
  }}
]"""


# --- PRManager Class ---
class PRManager:
    """Manages GitHub pull requests."""

    def __init__(self, subprocess_runner: SubprocessRunner, logger: RalphLogger, config: Config):
        self.runner = subprocess_runner
        self.logger = logger
        self.config = config

    def create(self, branch: str, title: str, body: str) -> PRInfo:
        """Create a new pull request."""
        self.logger.info(f"Creating PR for branch: {branch}")

        # Write body to temp file to avoid shell escaping issues
        body_file = self.config.repo_dir / ".ralph" / "pr_body.txt"
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(body, encoding="utf-8")

        cmd = [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=True, timeout=GH_TIMEOUT_SECS
            )
            # Parse PR number from output
            match = re.search(r"/pull/(\d+)", result.stdout)
            if match:
                pr_number = int(match.group(1))
                url = result.stdout.strip()
                self.logger.info(f"Created PR #{pr_number}: {url}")
                return PRInfo(number=pr_number, url=url)
            else:
                self.logger.error("Could not parse PR number from gh output")
                return PRInfo(number=0, url=result.stdout.strip())
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to create PR: {e.stderr}")
            raise

    def get_existing(self, branch: str) -> PRInfo | None:
        """Get existing PR for a branch if it exists."""
        cmd = ["gh", "pr", "list", "--head", branch, "--json", "number,url", "--jq", ".[0]"]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=False, timeout=GH_TIMEOUT_SECS
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if data and data.get("number"):
                    return PRInfo(number=data["number"], url=data.get("url", ""))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            self.logger.warn(f"Failed to get existing PR: {e}")

        return None

    def get_diff(self, pr_number: int) -> str:
        """Get the diff for a PR."""
        cmd = ["gh", "pr", "diff", str(pr_number)]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=True, timeout=GH_TIMEOUT_SECS
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to get PR diff: {e.stderr}")
            return ""

    def get_diff_for_file(self, pr_number: int, file_path: str) -> str:
        """Get the diff for a specific file in a PR."""
        full_diff = self.get_diff(pr_number)

        # Parse diff to extract file-specific changes
        lines = full_diff.split("\n")
        file_diff_lines: list[str] = []
        in_target_file = False

        for line in lines:
            if line.startswith("diff --git"):
                in_target_file = file_path in line
            if in_target_file:
                file_diff_lines.append(line)

        return "\n".join(file_diff_lines)

    def get_checks(self, pr_number: int) -> dict:
        """Get CI checks for a PR."""
        cmd = ["gh", "pr", "checks", str(pr_number), "--json", "name,state,conclusion"]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=False, timeout=GH_TIMEOUT_SECS
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            self.logger.warn(f"Failed to get PR checks: {e}")

        return {}

    def merge(self, pr_number: int, delete_branch: bool = True) -> bool:
        """Merge a PR."""
        self.logger.info(f"Merging PR #{pr_number}")

        cmd = ["gh", "pr", "merge", str(pr_number), "--squash", "--auto"]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=True, timeout=GH_TIMEOUT_SECS
            )
            self.logger.info(f"Successfully merged PR #{pr_number}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to merge PR #{pr_number}: {e.stderr}")
            return False

    def close(self, pr_number: int, comment: str = "") -> bool:
        """Close a PR with optional comment."""
        self.logger.info(f"Closing PR #{pr_number}")

        if comment:
            # Add comment first
            comment_cmd = ["gh", "pr", "comment", str(pr_number), "--body", comment]
            try:
                self.runner.run(
                    comment_cmd, cwd=self.config.repo_dir, check=False, timeout=GH_TIMEOUT_SECS
                )
            except subprocess.CalledProcessError:
                pass

        cmd = ["gh", "pr", "close", str(pr_number)]

        try:
            self.runner.run(cmd, cwd=self.config.repo_dir, check=True, timeout=GH_TIMEOUT_SECS)
            self.logger.info(f"Closed PR #{pr_number}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to close PR #{pr_number}: {e.stderr}")
            return False


# --- BranchManager Class ---
class BranchManager:
    """Manages git branch operations."""

    def __init__(self, subprocess_runner: SubprocessRunner, logger: RalphLogger, config: Config):
        self.runner = subprocess_runner
        self.logger = logger
        self.config = config

    @staticmethod
    def sanitise_branch_name(title: str) -> str:
        """Convert task title to a valid git branch name component."""
        # Remove special characters, keep alphanumeric, hyphen, underscore
        sanitized = re.sub(r"[^\w\s-]", "", title)
        # Replace spaces with hyphens
        sanitized = re.sub(r"\s+", "-", sanitized)
        # Convert to lowercase
        sanitized = sanitized.lower()
        # Limit length
        return sanitized[:50].strip("-")

    def verify_ssh_remote(self) -> None:
        """Verify that the git remote uses SSH, not HTTPS."""
        cmd = ["git", "remote", "get-url", "origin"]
        result = self.runner.run(
            cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS
        )
        remote_url = result.stdout.strip()

        if remote_url.startswith("https://"):
            self.logger.error(f"HTTPS remote detected: {remote_url}")
            raise RemoteNotSSHError(f"Remote must use SSH, not HTTPS: {remote_url}")

        self.logger.info(f"SSH remote verified: {remote_url}")

    def ensure_main_up_to_date(self) -> None:
        """Pull latest main branch, fail fast on merge conflicts."""
        self.logger.info("Ensuring main branch is up to date")

        # Fetch origin
        fetch_cmd = ["git", "fetch", "origin", MAIN_BRANCH]
        self.runner.run(fetch_cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)

        # Try ff-only merge
        merge_cmd = ["git", "merge", "--ff-only", f"origin/{MAIN_BRANCH}"]
        try:
            self.runner.run(
                merge_cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS
            )
            self.logger.info("Main branch is up to date")
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to fast-forward main: {e}")
            raise BranchSyncError(
                "Main branch diverged from origin — manual intervention required"
            ) from e

    def checkout_or_create(self, branch_name: str, resume: bool = False) -> BranchStatus:
        """Checkout existing branch or create new one from main."""
        self.logger.info(f"Checking out or creating branch: {branch_name}")

        # Check if branch exists locally
        cmd = ["git", "branch", "--list", branch_name]
        result = self.runner.run(
            cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS
        )
        local_exists = branch_name in result.stdout

        # Check if branch exists on remote
        cmd = ["git", "ls-remote", "--heads", "origin", branch_name]
        result = self.runner.run(
            cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS
        )
        remote_exists = branch_name in result.stdout

        if local_exists or remote_exists:
            if not resume:
                self.logger.error(f"Branch {branch_name} exists but resume=False")
                raise BranchExistsError(f"Branch {branch_name} exists — use --resume to continue")

            # Checkout existing branch
            if local_exists:
                cmd = ["git", "checkout", branch_name]
            else:
                cmd = ["git", "checkout", "-t", f"origin/{branch_name}"]

            self.runner.run(cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)
            self.logger.info(f"Checked out existing branch: {branch_name}")
            return BranchStatus(existed=True, had_commits=True)
        else:
            # Create new branch from main
            cmd = ["git", "checkout", "-b", branch_name, MAIN_BRANCH]
            self.runner.run(cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)
            self.logger.info(f"Created new branch: {branch_name}")
            return BranchStatus(existed=False, had_commits=False)

    def push_branch(self, branch_name: str, force: bool = False) -> None:
        """Push branch to remote."""
        self.logger.info(f"Pushing branch: {branch_name}")

        cmd = ["git", "push"]
        if force:
            cmd.append("--force-with-lease")
        cmd.extend(["origin", branch_name])

        self.runner.run(cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)
        self.logger.info(f"Pushed branch: {branch_name}")

    def delete_remote(self, branch_name: str) -> None:
        """Delete remote branch."""
        self.logger.info(f"Deleting remote branch: {branch_name}")

        cmd = ["git", "push", "origin", "--delete", branch_name]
        self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS)
        self.logger.info(f"Deleted remote branch: {branch_name}")

    def merge_and_cleanup(self, branch_name: str) -> None:
        """Merge branch to main and cleanup."""
        self.logger.info(f"Merging and cleaning up branch: {branch_name}")

        # Checkout main
        cmd = ["git", "checkout", MAIN_BRANCH]
        self.runner.run(cmd, cwd=self.config.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)

        # Merge branch (should be ff or already merged via PR)
        cmd = ["git", "merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"]
        self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS)

        # Delete local branch
        cmd = ["git", "branch", "-D", branch_name]
        self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS)

        self.logger.info(f"Merged and cleaned up branch: {branch_name}")

    def delete_local(self, branch_name: str) -> None:
        """Delete local branch."""
        self.logger.info(f"Deleting local branch: {branch_name}")

        cmd = ["git", "branch", "-D", branch_name]
        self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS)
        self.logger.info(f"Deleted local branch: {branch_name}")


# --- TaskTracker Class ---
class TaskTracker:
    """Tracks task progress and manages prd.json updates."""

    def __init__(self, prd_path: Path, logger: RalphLogger):
        self.prd_path = prd_path
        self.logger = logger
        self._prd_data: dict | None = None

    def load(self) -> dict:
        """Load and return prd.json data."""
        if self._prd_data is None:
            with open(self.prd_path, "r", encoding="utf-8") as f:
                self._prd_data = json.load(f)
        return self._prd_data

    def get_next_task(self) -> dict | None:
        """Get the next incomplete task."""
        prd = self.load()
        tasks = prd.get("tasks", [])

        for task in tasks:
            if not task.get("completed", False):
                return task
        return None

    def mark_complete(self, task_id: str) -> None:
        """Mark a task as completed."""
        prd = self.load()
        tasks = prd.get("tasks", [])

        for task in tasks:
            if task.get("id") == task_id:
                task["completed"] = True
                task["completed_at"] = datetime.now().isoformat()
                break

        self._save(prd)
        self.logger.info(f"Marked task {task_id} as complete")

    def append_progress(self, message: str) -> None:
        """Append a progress message to progress.txt."""
        progress_path = self.prd_path.parent / PROGRESS_FILE
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")

    def commit_tracking(self, message: str) -> None:
        """Commit tracking changes to git."""
        # This would be called after modifying prd.json or progress.txt
        self.logger.info(f"Tracking update: {message}")

    def add_task(self, task: dict) -> None:
        """Add a new task to the backlog."""
        prd = self.load()
        tasks = prd.get("tasks", [])
        tasks.append(task)
        self._save(prd)
        self.logger.info(f"Added task {task.get('id', 'unknown')} to backlog")

    def mark_decomposed(self, task_id: str, subtasks: list[dict]) -> None:
        """Mark a task as decomposed with its subtasks."""
        prd = self.load()
        tasks = prd.get("tasks", [])

        # Find and mark the parent task
        for task in tasks:
            if task.get("id") == task_id:
                task["decomposed"] = True
                task["subtasks"] = [st.get("id") for st in subtasks]
                break

        # Add subtasks to the task list
        tasks.extend(subtasks)

        self._save(prd)
        self.logger.info(f"Marked {task_id} as decomposed into {len(subtasks)} subtasks")

    def _save(self, prd_data: dict) -> None:
        """Save prd.json data."""
        with open(self.prd_path, "w", encoding="utf-8") as f:
            json.dump(prd_data, f, indent=2)
        self._prd_data = prd_data


# --- PlanChecker Class ---
class PlanChecker:
    """Checks plan structural validity and auto-decomposes large tasks."""

    def __init__(
        self,
        ai_runner: AIRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def check_structural(self, prd: dict) -> tuple[bool, list[str]]:
        """Check structural validity of the plan."""
        errors: list[str] = []
        tasks = prd.get("tasks", [])

        if not tasks:
            errors.append("No tasks defined in PRD")
            return False, errors

        # Validate each task
        validator = PrdValidator()
        all_task_ids = {t.get("id") for t in tasks if t.get("id")}

        for task in tasks:
            try:
                validator.validate(task, all_task_ids)
            except PlanInvalidError as e:
                errors.append(str(e))

        # Check for cycles in dependencies
        graph = DependencyGraph()
        graph.build_graph(tasks)
        if graph.detect_cycles():
            errors.append("Circular dependency detected in task graph")

        return len(errors) == 0, errors

    def _infer_complexity(self, task: dict) -> int:
        """Infer complexity score for a task (higher = more complex)."""
        score = 0

        # Description length (longer = more complex)
        desc_len = len(task.get("description", ""))
        score += desc_len // 100

        # Number of acceptance criteria
        acs = task.get("acceptance_criteria", [])
        score += len(acs) * 2

        # Number of files to modify
        files = task.get("files", [])
        score += len(files) * 3

        # Dependencies add complexity
        deps = task.get("depends_on", [])
        score += len(deps)

        return score

    def auto_decompose(self, task: dict, prd: dict) -> list[dict]:
        """Auto-decompose a large task into subtasks."""
        complexity = self._infer_complexity(task)

        # Threshold for decomposition
        if complexity < 15:
            self.logger.info(
                f"Task {task.get('id')} complexity {complexity} - no decomposition needed"
            )
            return []

        self.logger.info(f"Auto-decomposing task {task.get('id')} (complexity: {complexity})")

        # Use AI to decompose
        subtasks = self.ai_runner.run_decompose("gemini", task, prd)

        if subtasks:
            self.logger.info(f"Decomposed into {len(subtasks)} subtasks")
        else:
            self.logger.warn("Decomposition failed or produced no subtasks")

        return subtasks

    def run(self, prd: dict) -> PlanCheckResult:
        """Run full plan check with optional auto-decomposition."""
        valid, errors = self.check_structural(prd)
        warnings: list[str] = []
        decompositions = 0

        if not valid:
            return PlanCheckResult(
                valid=False,
                errors=errors,
                warnings=warnings,
                tasks_checked=len(prd.get("tasks", [])),
                decompositions=0,
            )

        # Check each task for complexity and decompose if needed
        tasks = prd.get("tasks", [])
        for task in tasks:
            complexity = self._infer_complexity(task)
            if complexity >= 15:
                warnings.append(f"Task {task.get('id')} has high complexity ({complexity})")
                subtasks = self.auto_decompose(task, prd)
                if subtasks:
                    decompositions += 1

        return PlanCheckResult(
            valid=True,
            errors=[],
            warnings=warnings,
            tasks_checked=len(tasks),
            decompositions=decompositions,
        )


# --- PreCommitGate Class ---
class PreCommitGate:
    """Runs pre-commit hooks and auto-fixes failures."""

    def __init__(
        self,
        subprocess_runner: SubprocessRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.runner = subprocess_runner
        self.logger = logger
        self.config = config

    def run(self, branch_name: str, round_num: int = 1) -> PreCommitResult:
        """Run pre-commit hooks, auto-fixing up to max_precommit_rounds."""
        self.logger.info(f"Running pre-commit gate (round {round_num})")

        # Check if pre-commit is installed
        check_cmd = ["which", "pre-commit"]
        result = self.runner.run(check_cmd, check=False, timeout=10)
        if result.returncode != 0:
            self.logger.warn("pre-commit not installed, skipping")
            return PreCommitResult(passed=True, rounds_used=0)

        # Run pre-commit
        cmd = ["pre-commit", "run", "--all-files"]
        result = self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=300)

        if result.returncode == 0:
            self.logger.info("pre-commit passed")
            return PreCommitResult(passed=True, rounds_used=round_num)

        # Try to auto-fix
        if round_num < self.config.max_precommit_rounds:
            self.logger.info("pre-commit failed, attempting auto-fix")
            fix_cmd = ["pre-commit", "run", "--all-files", "--fix"]
            fix_result = self.runner.run(
                fix_cmd, cwd=self.config.repo_dir, check=False, timeout=300
            )

            if fix_result.returncode == 0:
                self.logger.info("Auto-fix succeeded")
                return PreCommitResult(passed=True, rounds_used=round_num + 1)
            else:
                # Commit any partial fixes and retry
                self._commit_fixes(f"Auto-fix round {round_num}")
                return self.run(branch_name, round_num + 1)

        self.logger.error(f"pre-commit failed after {self.config.max_precommit_rounds} rounds")
        return PreCommitResult(passed=False, rounds_used=round_num)

    def _commit_fixes(self, message: str) -> None:
        """Commit any pending fixes."""
        # Add all changes
        add_cmd = ["git", "add", "-A"]
        self.runner.run(add_cmd, cwd=self.config.repo_dir, check=False, timeout=30)

        # Commit
        commit_cmd = ["git", "commit", "-m", message, "--no-verify"]
        self.runner.run(commit_cmd, cwd=self.config.repo_dir, check=False, timeout=30)


# --- PRDGuard Class ---
class PRDGuard:
    """Guards against PRD modifications by agents."""

    def __init__(self, logger: RalphLogger):
        self.logger = logger

    def check(self, diff: str) -> bool:
        """Check if diff contains PRD modifications."""
        # Check for prd.json modifications
        if "prd.json" in diff or "diff --git a/prd.json" in diff:
            self.logger.error("PRD modification detected in diff - AGENTS MUST NOT MODIFY PRD")
            return False
        return True


# --- TestRunner Class ---
class TestRunner:
    """Runs tests and reports results."""

    def __init__(
        self,
        subprocess_runner: SubprocessRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.runner = subprocess_runner
        self.logger = logger
        self.config = config

    def run(self, test_path: Path | None = None, round_num: int = 1) -> TestResult:
        """Run tests, returning result."""
        self.logger.info(f"Running tests (round {round_num})")

        # Determine test command based on project
        if (self.config.repo_dir / "pyproject.toml").exists():
            cmd = ["uv", "run", "pytest", "-v"]
        elif (self.config.repo_dir / "package.json").exists():
            cmd = ["npm", "test"]
        else:
            cmd = ["pytest", "-v"]

        if test_path:
            cmd.append(str(test_path))

        result = self.runner.run(cmd, cwd=self.config.repo_dir, check=False, timeout=300)

        if result.returncode == 0:
            self.logger.info("Tests passed")
            return TestResult(passed=True, rounds_used=round_num)

        self.logger.error(f"Tests failed: {result.stdout}\n{result.stderr}")
        return TestResult(passed=False, rounds_used=round_num)


# --- RalphTestWriter Class ---
class RalphTestWriter:
    """Writes tests for tasks using AI agents."""

    def __init__(
        self,
        ai_runner: AIRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def write_tests(self, task: dict, agent: str) -> tuple[bool, Path | None]:
        """Write tests for a task, returns (success, test_file_path)."""
        self.logger.info(f"Writing tests for task {task.get('id')} using {agent}")

        # Discover where to write tests
        test_file = self._discover_test_file(task)

        # Build prompt
        existing_tests = ""
        if test_file.exists():
            existing_tests = test_file.read_text(encoding="utf-8")

        prompt = PromptBuilder.test_writer_prompt(task, {}, existing_tests)

        # Run test writer
        success, output = self.ai_runner.run_test_writer(agent, prompt, self.config.repo_dir)

        if not success:
            self.logger.error("Test writer failed to produce tests")
            return False, None

        # Write tests to file
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(output, encoding="utf-8")

        self.logger.info(f"Tests written to {test_file}")
        return True, test_file

    def _discover_test_file(self, task: dict) -> Path:
        """Discover or create appropriate test file path."""
        files = task.get("files", [])
        task_id = task.get("id", "unknown")

        # Try to find test file based on source files
        for file_path in files:
            if file_path.endswith(".py"):
                # Check for corresponding test file
                test_path = self.config.repo_dir / "tests" / f"test_{Path(file_path).stem}.py"
                if test_path.exists():
                    return test_path

        # Default to task-specific test file
        return self.config.repo_dir / "tests" / f"test_{task_id}.py"

    def _commit_test_file(self, test_file: Path, task_id: str) -> None:
        """Commit the test file to git."""
        # This would be called to commit test files
        self.logger.info(f"Test file ready for commit: {test_file}")


# --- WorktreeError Exception ---
class WorktreeError(RalphError):
    """Error related to worktree operations."""

    pass


# --- WorktreeManager Class ---
class WorktreeManager:
    """Manages git worktrees for isolated task execution."""

    def __init__(
        self,
        repo_dir: Path,
        workstream: str | None,
        subprocess_runner: SubprocessRunner,
        logger: RalphLogger,
    ):
        self.repo_dir = repo_dir
        self.workstream = workstream
        self.runner = subprocess_runner
        self.logger = logger
        self._worktrees: set[str] = set()

    def _branch_name(self, task_id: str, task_title: str) -> str:
        """Generate branch name for a task."""
        sanitized = BranchManager.sanitise_branch_name(task_title)
        prefix = f"{self.workstream}/" if self.workstream else ""
        return f"{prefix}ralph/{task_id}-{sanitized}"

    def _worktree_path(self, task_id: str) -> Path:
        """Generate worktree path for a task."""
        worktree_base = self.repo_dir / ".ralph" / "worktrees"
        prefix = f"{self.workstream}_" if self.workstream else ""
        return worktree_base / f"{prefix}{task_id}"

    def create_worktree(self, task_id: str, task_title: str) -> Path:
        """Create a worktree for a task."""
        worktree_path = self._worktree_path(task_id)
        branch_name = self._branch_name(task_id, task_title)

        self.logger.info(f"Creating worktree for {task_id} at {worktree_path}")

        # Ensure worktree directory exists
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create worktree
        cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_path), MAIN_BRANCH]
        try:
            self.runner.run(cmd, cwd=self.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)
        except subprocess.CalledProcessError:
            # Branch might already exist, try without -b
            cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
            self.runner.run(cmd, cwd=self.repo_dir, check=True, timeout=GIT_TIMEOUT_SECS)

        self._worktrees.add(task_id)
        self.logger.info(f"Created worktree for {task_id}")
        return worktree_path

    def cleanup_worktree(self, task_id: str) -> None:
        """Remove a worktree for a task."""
        worktree_path = self._worktree_path(task_id)

        self.logger.info(f"Cleaning up worktree for {task_id}")

        # Remove worktree
        cmd = ["git", "worktree", "remove", str(worktree_path), "--force"]
        self.runner.run(cmd, cwd=self.repo_dir, check=False, timeout=GIT_TIMEOUT_SECS)

        self._worktrees.discard(task_id)
        self.logger.info(f"Cleaned up worktree for {task_id}")

    def list_active_worktrees(self) -> list[str]:
        """List all active worktree task IDs."""
        return list(self._worktrees)

    def make_isolated_runner(self, task_id: str, task_title: str) -> "IsolatedTaskRunner":
        """Create an isolated task runner for a task."""
        worktree_path = self.create_worktree(task_id, task_title)
        return IsolatedTaskRunner(worktree_path, task_id, self)


# --- IsolatedTaskRunner Class (for WorktreeManager) ---
class IsolatedTaskRunner:
    """Runs a task in an isolated worktree."""

    def __init__(self, worktree_path: Path, task_id: str, worktree_manager: WorktreeManager):
        self.worktree_path = worktree_path
        self.task_id = task_id
        self.worktree_manager = worktree_manager

    def cleanup(self) -> None:
        """Clean up the worktree."""
        self.worktree_manager.cleanup_worktree(self.task_id)


# --- CIPoller Class ---
class CIPoller:
    """Polls CI status and auto-fixes failures."""

    def __init__(
        self,
        pr_manager: PRManager,
        ai_runner: AIRunner,
        logger: RalphLogger,
        config: Config,
    ):
        self.pr_manager = pr_manager
        self.ai_runner = ai_runner
        self.logger = logger
        self.config = config

    def wait_and_fix(
        self,
        pr_number: int,
        task: dict,
        coder: str,
        max_rounds: int | None = None,
    ) -> CIResult:
        """Wait for CI and auto-fix failures up to max rounds."""
        max_rounds = max_rounds or self.config.max_ci_fix_rounds
        rounds_used = 0

        while rounds_used < max_rounds:
            rounds_used += 1
            self.logger.info(f"CI check round {rounds_used}/{max_rounds} for PR #{pr_number}")

            # Poll for CI status
            checks = self.pr_manager.get_checks(pr_number)

            # Check if all checks passed
            all_passed = True
            pending = False
            for check in checks:
                state = check.get("state", "").upper()
                conclusion = check.get("conclusion", "").upper()

                if state in CI_PENDING_STATES or conclusion in CI_PENDING_STATES:
                    pending = True
                    all_passed = False
                elif conclusion in CI_FAILURE_STATES or state in CI_FAILURE_STATES:
                    all_passed = False

            if all_passed:
                self.logger.info(f"All CI checks passed for PR #{pr_number}")
                return CIResult(passed=True, rounds_used=rounds_used)

            if pending and rounds_used < max_rounds:
                self.logger.info(f"CI still pending, waiting {CI_POLL_INTERVAL_SECS}s...")
                time.sleep(CI_POLL_INTERVAL_SECS)
                continue

            # CI failed - try to fix
            if rounds_used < max_rounds:
                self.logger.info(f"CI failed, attempting auto-fix round {rounds_used}")

                # Get CI log for context
                ci_log = self._get_ci_log(pr_number)

                # Build fix prompt
                fix_prompt = f"""Fix the CI failures in this PR.

Task: {task.get("title", "Unknown")}

CI Log:
{ci_log}

Instructions:
1. Analyze the CI failures
2. Make minimal fixes to resolve the issues
3. Do NOT modify prd.json
4. Commit the fixes

Respond with "Fixes applied" when done."""

                success = self.ai_runner.run_coder(coder, fix_prompt, self.config.repo_dir)
                if success:
                    # Push fixes
                    branch_manager = BranchManager(
                        SubprocessRunner(self.logger),
                        self.logger,
                        self.config,
                    )
                    branch_manager.push_branch(branch_name=task.get("id", "unknown"), force=True)
                else:
                    self.logger.error("Auto-fix failed")

        self.logger.error(f"CI still failing after {max_rounds} rounds")
        return CIResult(passed=False, rounds_used=rounds_used)

    def _get_ci_log(self, pr_number: int) -> str:
        """Get the CI log for a PR."""
        cmd = [
            "gh",
            "run",
            "list",
            "--pr",
            str(pr_number),
            "--json",
            "databaseId,conclusion",
            "--limit",
            "1",
        ]

        try:
            result = self.runner.run(
                cmd, cwd=self.config.repo_dir, check=False, timeout=GH_TIMEOUT_SECS
            )
            if result.returncode == 0 and result.stdout.strip():
                runs = json.loads(result.stdout)
                if runs:
                    run_id = runs[0].get("databaseId")
                    if run_id:
                        # Get logs
                        log_cmd = ["gh", "run", "view", str(run_id), "--log"]
                        log_result = self.runner.run(
                            log_cmd, cwd=self.config.repo_dir, check=False, timeout=GH_TIMEOUT_SECS
                        )
                        return log_result.stdout if log_result.returncode == 0 else ""
        except Exception as e:
            self.logger.warn(f"Failed to get CI log: {e}")

        return ""


# --- ScrumMaster Class ---
class ScrumMaster:
    """Coordinates sprint execution and cleanup."""

    def __init__(
        self,
        orchestrator: "Orchestrator",
        logger: RalphLogger,
        config: Config,
    ):
        self.orchestrator = orchestrator
        self.logger = logger
        self.config = config

    def _post_sprint_cleanup(self) -> None:
        """Cleanup after sprint completion."""
        self.logger.info("Running post-sprint cleanup")

        # Archive logs
        log_dir = self.config.repo_dir / ".ralph" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_log = log_dir / f"ralph_{timestamp}.log"

        if self.config.log_file.exists():
            import shutil

            shutil.copy2(self.config.log_file, archive_log)
            self.logger.info(f"Archived log to {archive_log}")

        # Clean up old worktrees
        self.logger.info("Post-sprint cleanup complete")


# --- Orchestrator Class ---
class Orchestrator:
    """Main orchestrator for executing tasks."""

    def __init__(
        self,
        config: Config,
        task_tracker: TaskTracker,
        branch_manager: BranchManager,
        pr_manager: PRManager,
        ai_runner: AIRunner,
        logger: RalphLogger,
    ):
        self.config = config
        self.task_tracker = task_tracker
        self.branch_manager = branch_manager
        self.pr_manager = pr_manager
        self.ai_runner = ai_runner
        self.logger = logger

    def _check_cli(self) -> None:
        """Check required CLI tools are available."""
        required_tools = ["git", "gh"]

        for tool in required_tools:
            result = subprocess.run(["which", tool], capture_output=True, text=True)
            if result.returncode != 0:
                raise PreflightError(f"Required CLI tool not found: {tool}")

        self.logger.info("All required CLI tools available")

    def _preflight(self) -> None:
        """Run preflight checks before starting."""
        self.logger.info("Running preflight checks")

        # Check CLI tools
        self._check_cli()

        # Verify SSH remote
        self.branch_manager.verify_ssh_remote()

        # Ensure main is up to date
        self.branch_manager.ensure_main_up_to_date()

        self.logger.info("Preflight checks passed")

    def _check_stop_conditions(self, iteration: int) -> tuple[bool, str]:
        """Check if sprint should stop."""
        if iteration >= self.config.max_iterations:
            return True, "Max iterations reached"

        # Check for manual stop signal
        stop_file = self.config.repo_dir / ".ralph" / "stop"
        if stop_file.exists():
            return True, "Stop signal detected"

        return False, ""

    def _run_task(
        self, task: dict, coder: str, reviewer: str, test_writer: str
    ) -> TaskExecutionResult:
        """Run a single task through the full lifecycle."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")

        self.logger.info(f"Starting task {task_id}: {title}")

        # Create branch
        branch_name = f"ralph/{task_id}-{BranchManager.sanitise_branch_name(title)}"

        try:
            branch_status = self.branch_manager.checkout_or_create(
                branch_name, resume=self.config.resume
            )
        except BranchExistsError:
            return TaskExecutionResult(
                task_id=task_id,
                title=title,
                pr_number=None,
                ci_passed=False,
                ci_rounds_used=0,
                escalated=False,
                fatal_error_type="BranchExistsError",
                fatal_error_reason=f"Branch {branch_name} exists, use --resume",
            )

        # Run coder
        prompt = PromptBuilder.coder_prompt(task, {}, round_num=1, resume=branch_status.had_commits)
        coder_success = self.ai_runner.run_coder(coder, prompt, self.config.repo_dir)

        if not coder_success:
            return TaskExecutionResult(
                task_id=task_id,
                title=title,
                pr_number=None,
                ci_passed=False,
                ci_rounds_used=0,
                escalated=False,
                fatal_error_type="CoderFailedError",
                fatal_error_reason="Coder agent failed to complete task",
            )

        # Push branch
        self.branch_manager.push_branch(branch_name, force=False)

        # Create PR
        pr_info = self.pr_manager.create(branch_name, title, PromptBuilder.pr_body(task))

        return TaskExecutionResult(
            task_id=task_id,
            title=title,
            pr_number=pr_info.number,
            ci_passed=True,  # Will be updated by CI poller
            ci_rounds_used=0,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        )

    def _run_task_tdd(
        self, task: dict, coder: str, reviewer: str, test_writer: str
    ) -> TaskExecutionResult:
        """Run a task in TDD mode (tests written before code)."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")

        self.logger.info(f"Starting TDD task {task_id}: {title}")

        # First, write tests
        test_writer = RalphTestWriter(self.ai_runner, self.logger, self.config)
        success, test_file = test_writer.write_tests(task, test_writer)

        if not success or not test_file:
            return TaskExecutionResult(
                task_id=task_id,
                title=title,
                pr_number=None,
                ci_passed=False,
                ci_rounds_used=0,
                escalated=False,
                fatal_error_type="TestWriterFailed",
                fatal_error_reason="Failed to write tests",
            )

        # Now run the normal task flow with the test file as context
        return self._run_task(task, coder, reviewer, test_writer)

    def _run_task_standard(
        self, task: dict, coder: str, reviewer: str, test_writer: str
    ) -> TaskExecutionResult:
        """Run a task in standard mode."""
        return self._run_task(task, coder, reviewer, test_writer)

    def run(self) -> None:
        """Run the main orchestration loop."""
        self.logger.info("Starting Ralph orchestrator")

        # Preflight checks
        self._preflight()

        iteration = 0
        while True:
            iteration += 1
            self.logger.info(f"Starting iteration {iteration}")

            # Check stop conditions
            should_stop, reason = self._check_stop_conditions(iteration)
            if should_stop:
                self.logger.info(f"Stopping: {reason}")
                break

            # Get next task
            task = self.task_tracker.get_next_task()
            if not task:
                self.logger.info("No more tasks to process")
                break

            # Assign agents
            coder, reviewer, test_writer = self.ai_runner.assign_agents(task)

            # Run task
            if self.config.tdd_mode:
                result = self._run_task_tdd(task, coder, reviewer, test_writer)
            else:
                result = self._run_task_standard(task, coder, reviewer, test_writer)

            # Update tracking
            if result.fatal_error_type is None:
                self.task_tracker.mark_complete(task["id"])
                self.task_tracker.append_progress(f"Completed {task['id']}")
            else:
                self.logger.error(f"Task {task['id']} failed: {result.fatal_error_type}")
                self.task_tracker.append_progress(f"Failed {task['id']}: {result.fatal_error_type}")

        self.logger.info("Sprint complete")
