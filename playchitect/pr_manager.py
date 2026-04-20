"""PRManager — all gh pr operations for ralph.py."""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ralph import RalphLogger, SubprocessRunner

GH_TIMEOUT_SECS = 60


@dataclass
class PRInfo:
    """Data class holding PR number and URL."""

    number: int
    url: str


class PRManager:
    """All gh pr operations. Parses PR numbers with regex, not grep."""

    def __init__(self, runner: SubprocessRunner, logger: RalphLogger):
        self.runner = runner
        self.logger = logger

    def create(self, branch: str, title: str, body: str) -> PRInfo:
        r"""
        Create a PR for the given branch.

        Runs gh pr create, parses PR number with re.search(r'(\d+)$', url),
        returns PRInfo.
        """
        result = self.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            timeout=GH_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            self.logger.error(f"gh pr create failed: {result.stderr}")
            raise Exception(f"gh pr create failed: {result.stderr}")

        output = result.stdout.strip()
        match = re.search(r"(\d+)$", output)
        if not match:
            self.logger.error(f"Could not parse PR number from output: {output}")
            raise Exception(f"Could not parse PR number from output: {output}")

        pr_number = int(match.group(1))
        url = f"https://github.com/owner/repo/pull/{pr_number}"

        return PRInfo(number=pr_number, url=url)

    def get_existing(self, branch: str) -> PRInfo | None:
        """
        Get the existing open PR for a branch.

        Returns PRInfo if one exists, None otherwise.
        """
        result = self.runner.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url"],
            timeout=GH_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            return None

        try:
            prs = json.loads(result.stdout)
            if not prs:
                return None
            return PRInfo(number=prs[0]["number"], url=prs[0]["url"])
        except (json.JSONDecodeError, IndexError, KeyError):
            return None

    def get_diff(self, pr_number: int, retries: int = 5, delay: int = 10) -> str:
        """
        Get the diff for a PR.

        Retries with delay if diff is empty (handles fresh PR race condition).
        """
        for attempt in range(retries):
            result = self.runner.run(
                ["gh", "pr", "diff", str(pr_number)],
                timeout=GH_TIMEOUT_SECS,
            )
            if result.returncode != 0:
                self.logger.error(f"gh pr diff failed: {result.stderr}")
                raise Exception(f"gh pr diff failed: {result.stderr}")

            diff = result.stdout
            if diff.strip():
                return diff

            if attempt < retries - 1:
                self.logger.info(
                    f"Empty diff for PR #{pr_number}, retrying in {delay}s (attempt {attempt + 1}/{retries})"
                )
                time.sleep(delay)

        return ""

    def get_diff_for_file(self, pr_number: int, filepath: str) -> str:
        """
        Get the diff for a specific file in a PR.

        Gets full diff and extracts the relevant section.
        """
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

    def get_checks(self, pr_number: int) -> list[dict[str, Any]]:
        """
        Get CI checks for a PR.

        Returns parsed JSON from 'gh pr checks --json name,state,conclusion,required'.
        """
        result = self.runner.run(
            ["gh", "pr", "checks", str(pr_number), "--json", "name,state,conclusion,required"],
            timeout=GH_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            self.logger.error(f"gh pr checks failed: {result.stderr}")
            raise Exception(f"gh pr checks failed: {result.stderr}")

        try:
            data = json.loads(result.stdout)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse gh pr checks JSON: {e}")
            raise Exception(f"Failed to parse gh pr checks JSON: {e}") from e

    def merge(self, pr_number: int) -> None:
        """Merge a PR using squash merge."""
        result = self.runner.run(
            ["gh", "pr", "merge", str(pr_number), "--squash", "--auto"],
            timeout=GH_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            self.logger.error(f"gh pr merge failed: {result.stderr}")
            raise Exception(f"gh pr merge failed: {result.stderr}")

    def close(self, pr_number: int, reason: str) -> None:
        """Close a PR with a comment explaining the reason."""
        comment_result = self.runner.run(
            ["gh", "pr", "comment", str(pr_number), "--body", reason],
            timeout=GH_TIMEOUT_SECS,
        )
        if comment_result.returncode != 0:
            self.logger.error(f"gh pr comment failed: {comment_result.stderr}")

        close_result = self.runner.run(
            ["gh", "pr", "close", str(pr_number)],
            timeout=GH_TIMEOUT_SECS,
        )
        if close_result.returncode != 0:
            self.logger.error(f"gh pr close failed: {close_result.stderr}")
            raise Exception(f"gh pr close failed: {close_result.stderr}")
