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
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# --- Constants ---

DEFAULT_MAX_ITERATIONS = 10
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
DEFAULT_OPENCODE_MODEL = "opencode/kimi-k2.5"
GEMINI_MODEL = "gemini-2.5-pro"


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


class AgentSandboxViolation(RalphError):  # noqa: N818
    """Agent attempted an operation outside its sandbox."""

    pass


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
class ReviewQualityResult:
    acceptable: bool
    reason: str  # why it failed quality check (if it did)


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
    ):
        self.prd_path = prd_path
        self.progress_path = progress_path
        self.runner = runner
        self.logger = logger

    def load(self) -> dict:
        """Reads and returns prd.json from disk."""
        with open(self.prd_path, "r", encoding="utf-8") as f:
            return json.load(f)

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
        """
        prd = self.load()
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
        """Counts incomplete ralph-owned non-decomposed tasks."""
        prd = self.load()
        count = 0
        for task in prd.get("tasks", []):
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

    def mark_complete(self, task_id: str) -> None:
        """Fresh load, sets completed=true, writes back."""
        prd = self.load()
        found = False
        for task in prd.get("tasks", []):
            if task["id"] == task_id:
                if task.get("completed"):
                    raise PRDGuardViolation(
                        f"task {task_id} already marked complete — possible bulk-marking attack"
                    )
                task["completed"] = True
                found = True
                break
        if not found:
            raise PRDGuardViolation(f"task {task_id} not found")
        self._save(prd)

    def append_progress(self, task_id: str, title: str, pr_number: int, today: str) -> None:
        """Appends a formatted line to progress.txt."""
        line = f"{today} | {task_id} | {title} | PR #{pr_number}\n"
        with open(self.progress_path, "a", encoding="utf-8") as f:
            f.write(line)

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


class PlanChecker:
    """
    Validates the plan before the sprint starts.
    Structural validation, complexity inference, and auto-decomposition.
    """

    def __init__(self, task_tracker: TaskTracker, ai_runner, logger: RalphLogger):
        self.task_tracker = task_tracker
        self.ai_runner = ai_runner
        self.logger = logger

    def check_structural(self, prd: dict) -> list[str]:
        """Validates required fields, non-empty ACs, and resolved dependencies."""
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
        completed_ids = {t["id"] for t in tasks if t.get("completed")}

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
                elif dep not in completed_ids:
                    errors.append(f"{task_id}: depends_on incomplete task '{dep}'")

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

        # ai_check (M2 feature) produces warnings only
        warnings = []
        if ai_check:
            # warnings = self.ai_runner.check_ai(prd["tasks"])
            pass

        decompositions = self.auto_decompose(prd)

        return PlanCheckResult(
            valid=True,
            errors=[],
            warnings=warnings,
            tasks_checked=sum(1 for t in prd.get("tasks", []) if not t.get("completed")),
            decompositions=decompositions,
        )


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
            ["gh", "pr", "create", "--branch", branch, "--title", title, "--body", body],
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
            return "opencode", "claude", "gemini"

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
        text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
        text = re.sub(r"\x1b[@-Z\\-_]", "", text)

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

    def run_coder(self, agent: str, prompt: str, cwd: Path) -> bool:
        """Invokes the agent subprocess, returns True on success."""
        self.logger.info(f"Invoking coder: {agent}")
        try:
            if agent == "claude":
                self.runner.run(
                    ["claude", "--dangerously-skip-permissions", "--print", prompt],
                    env_removals=["CLAUDECODE"],
                    cwd=cwd,
                    check=True,
                )
            elif agent == "gemini":
                self.runner.run(
                    ["gemini", "-m", GEMINI_MODEL, "--yolo", "-p", prompt],
                    cwd=cwd,
                    check=True,
                )
            else:  # opencode
                self.runner.run(
                    [
                        "opencode",
                        "run",
                        "-m",
                        self.config.opencode_model,
                        "--dangerously-skip-permissions",
                        prompt,
                    ],
                    cwd=cwd,
                    check=True,
                )
            return True
        except subprocess.CalledProcessError:
            self.logger.error(f"Coder {agent} failed.")
            return False

    def run_reviewer(self, agent: str, prompt: str) -> str:
        """Returns reviewer output; handles nested-Claude fallback."""
        if agent == "claude" and self._is_nested_claude_session():
            self.logger.warn(
                "Nested Claude session detected — claude reviewer unavailable."
                " Falling back to gemini."
            )
            return self.run_reviewer("gemini", prompt)

        self.logger.info(f"Invoking reviewer: {agent}")
        try:
            if agent == "claude":
                result = self.runner.run(
                    ["claude", "--print", prompt],
                    env_removals=["CLAUDECODE"],
                    check=True,
                )
            elif agent == "gemini":
                result = self.runner.run(
                    ["gemini", "-m", GEMINI_MODEL, "-p", prompt],
                    check=True,
                )
            else:  # opencode
                result = self.runner.run(
                    ["opencode", "run", "-m", self.config.opencode_model, prompt],
                    timeout=300,
                    check=True,
                )
            return self._clean_output(result.stdout)
        except subprocess.CalledProcessError:
            self.logger.error(f"Reviewer {agent} failed.")
            return ""

    def run_test_writer(self, prompt: str, cwd: Path, agent: str | None = None) -> bool:
        """Test writer always uses a different model from coder."""
        if agent is None:
            agent = "gemini" if self._is_nested_claude_session() else "claude"
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
    """
    Polls CI completion using run-ID pinning to avoid stale-data race.
    On failure: fetches logs, invokes coder fix loop, re-polls.
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

    def _get_latest_run_id(self, branch: str) -> str:
        """Gets the latest run ID for a branch using gh run list.

        Runs: gh run list --branch <branch> --json databaseId --jq .[0].databaseId

        Returns the run ID or raises CITimeoutError if no run is found.
        """
        result = self.runner.run(
            [
                "gh",
                "run",
                "list",
                "--branch",
                branch,
                "--json",
                "databaseId",
                "--jq",
                ".[0].databaseId",
            ],
            check=True,
        )
        run_id = result.stdout.strip()
        if not run_id:
            raise CITimeoutError(f"No run found for branch {branch}")
        return run_id

    def _wait_for_run(self, run_id: str) -> str:
        """Polls a specific run ID until completion.

        Polls 'gh run view <run_id> --json status,conclusion' every CI_POLL_INTERVAL_SECS.
        Returns 'PASSED' or 'FAILED'. Raises CITimeoutError after CI_POLL_MAX_ATTEMPTS.
        """
        attempts = 0
        while attempts < CI_POLL_MAX_ATTEMPTS:
            result = self.runner.run(
                [
                    "gh",
                    "run",
                    "view",
                    run_id,
                    "--json",
                    "status,conclusion",
                ],
                check=True,
            )
            data = json.loads(result.stdout)
            status = data.get("status", "")
            conclusion = data.get("conclusion")

            if status in CI_PENDING_STATES:
                self.logger.info(f"Run {run_id} status: {status} (attempt {attempts + 1})")
                time.sleep(CI_POLL_INTERVAL_SECS)
                attempts += 1
                continue

            if conclusion in CI_FAILURE_STATES:
                return "FAILED"

            if conclusion == "success":
                return "PASSED"

            return conclusion or "UNKNOWN"

        raise CITimeoutError(
            f"CI run {run_id} did not complete after "
            f"{CI_POLL_MAX_ATTEMPTS * CI_POLL_INTERVAL_SECS // 60} minutes"
        )

    def _wait_for_new_run(self, branch: str, prev_run_id: str, timeout: int = 180) -> str:
        """Polls gh run list every 10s until a different run ID appears.

        Returns new run ID. Raises CITimeoutError if no new run after timeout seconds.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                new_run_id = self._get_latest_run_id(branch)
                if new_run_id != prev_run_id:
                    self.logger.info(f"New run detected: {new_run_id}")
                    return new_run_id
            except Exception:
                pass

            self.logger.info(f"Waiting for new run (current: {prev_run_id})...")
            time.sleep(10)

        raise CITimeoutError(f"No new run appeared for branch {branch} after {timeout}s")

    def wait_for_completion(self, pr_number: int, branch: str) -> CIResult:
        """Waits for CI to complete using run-ID pinning.

        Uses the 'conclusion' field (not 'state') for pass/fail determination.
        Treats empty checks list as PENDING.
        """
        self.logger.info(f"Waiting for CI on PR #{pr_number} (branch: {branch})")

        try:
            run_id = self._get_latest_run_id(branch)
        except CITimeoutError:
            self.logger.info("No run found yet — treating as PENDING")
            return CIResult(passed=False, rounds_used=0)

        self.logger.info(f"Found run ID: {run_id}")

        conclusion = self._wait_for_run(run_id)

        if conclusion == "PASSED":
            self.logger.info("CI passed")
            return CIResult(passed=True, rounds_used=1)

        self.logger.error(f"CI failed with conclusion: {conclusion}")
        return CIResult(passed=False, rounds_used=1)

    def _check_required_failures(self, pr_number: int) -> tuple[bool, list[str]]:
        """Checks required CI checks for failures.

        Filters to only required=true checks. Optional check failures log warning only.
        Returns (has_required_failure, list of failing required check names).
        On any error fetching checks, returns (False, []) — fail-open to avoid blocking.
        """
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
        """Waits for CI, on failure invokes coder fix loop, re-polls.

        Filters CI checks to only block on required=true checks.
        Optional failing checks log warning only.
        """
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

            self.logger.info("Pushing fix and waiting for new run...")

            try:
                current_run_id = self._get_latest_run_id(branch)
            except CITimeoutError:
                current_run_id = ""

            branch_manager = BranchManager(self.config.repo_dir, self.runner, self.logger)
            branch_manager.push_branch(branch)

            self._wait_for_new_run(branch, current_run_id, timeout=180)

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

        self._nested_claude_warning_issued = False

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
            self.plan_checker.run(prd)
        except PlanInvalidError as e:
            raise PreflightError(f"Plan validation failed: {e}") from e

        self.logger.info("Preflight passed.")

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
        pr_info: PRInfo,
    ) -> TaskResult:
        """Standard mode state machine."""
        try:
            self.branch_manager.ensure_main_up_to_date()
        except BranchSyncError as e:
            return TaskResult(fatal=True, message=str(e))

        try:
            self.branch_manager.checkout_or_create(branch, self.config.resume)
        except BranchExistsError as e:
            return TaskResult(fatal=True, message=str(e))

        branch_status = self.branch_manager.checkout_or_create(branch, self.config.resume)

        try:
            prompt = PromptBuilder.coder_prompt(task, coder, prd, resume=branch_status.had_commits)
            success = self.ai_runner.run_coder(coder, prompt, self.config.repo_dir)
        except Exception as e:
            self.logger.error(f"Coder failed: {e}")
            return TaskResult(fatal=True, message=str(e))

        if not success:
            return TaskResult(fatal=True, message="Coder execution failed")

        self.precommit_gate.run(task, prd, self.config.repo_dir)

        self.test_runner.run(task, prd)

        try:
            self.branch_manager.push_branch(branch)
        except subprocess.CalledProcessError as e:
            return TaskResult(fatal=True, message=f"Push failed: {e}")

        self.logger.info("Waiting for CI...")
        try:
            self.ci_poller.wait_and_fix(task, pr_info.number, branch, prd)
        except (CITimeoutError, CIFailedFatal) as e:
            self.logger.error(f"CI failed: {e}")
            self.pr_manager.close(pr_info.number, f"CI failed: {e}")
            return TaskResult(fatal=True, message=str(e))

        try:
            self.prd_guard.check(pr_info.number)
        except PRDGuardViolation as e:
            self.logger.error(f"PRDGuard violation: {e}")
            self.pr_manager.close(pr_info.number, str(e))
            raise

        try:
            self.pr_manager.merge(pr_info.number)
        except subprocess.CalledProcessError as e:
            return TaskResult(fatal=True, message=f"Merge failed: {e}")

        self.branch_manager.merge_and_cleanup(branch)

        now = datetime.now().strftime("%Y-%m-%d")
        self.task_tracker.append_progress(task["id"], task["title"], pr_info.number, now)
        self.task_tracker.mark_complete(task["id"])

        try:
            self.task_tracker.commit_tracking(task["id"], task["title"])
        except subprocess.CalledProcessError as e:
            self.logger.warn(f"Tracking commit failed: {e}")

        return TaskResult(fatal=False)

    def _run_task_tdd(
        self,
        task: dict,
        branch: str,
        prd: dict,
        coder: str,
        reviewer: str,
        pr_info: PRInfo,
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

        pr_body = PromptBuilder.pr_body(task)

        try:
            existing = self.pr_manager.get_existing(branch)
            if existing:
                self.logger.info(f"Resuming existing PR #{existing.number}")
                pr_info = existing
            else:
                pr_info = self.pr_manager.create(branch, task["title"], pr_body)
        except subprocess.CalledProcessError as e:
            return TaskResult(fatal=True, message=f"PR creation failed: {e}")

        if self.config.tdd_mode:
            return self._run_task_tdd(task, branch, prd, coder, reviewer, pr_info)

        return self._run_task_standard(task, branch, prd, coder, reviewer, pr_info)

    def run(self, max_iterations: int) -> None:
        """Main loop: preflight, get next task, run until stop or max."""
        prd = self.task_tracker.load()

        try:
            self._preflight(prd)
        except PreflightError as e:
            self.logger.fatal(f"Preflight failed: {e}")

        prd = self.task_tracker.load()

        for iteration in range(1, max_iterations + 1):
            task = self.task_tracker.get_next_task()

            stop_reason = self._check_stop_conditions(task)
            if stop_reason:
                self.logger.info(f"Stopping: {stop_reason}")
                self.logger.info("Loop finished.")
                return

            self.logger.info(
                f"Iteration {iteration}/{max_iterations}: {task['id']} {task['title']}"
            )

            branch = f"ralph/{task['id']}-{self.branch_manager.sanitise_branch_name(task['title'])}"

            result = self._run_task(task, branch, prd)

            if result.fatal:
                self.logger.error(f"Task failed: {result.message}")
                self.logger.info("Loop stopped due to fatal error.")
                return

            prd = self.task_tracker.load()

        self.logger.info(f"Max iterations ({max_iterations}) reached.")
        self.logger.info("Loop finished.")


def main() -> int:
    return 0


if __name__ == "__main__":
    sys.exit(main())
