from pathlib import Path
from unittest.mock import MagicMock

from ralph import PRInfo, ScrumMaster

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scrum_master(tmp_path: Path) -> ScrumMaster:
    branch_manager = MagicMock()
    pr_manager = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    return ScrumMaster(
        branch_manager=branch_manager,
        pr_manager=pr_manager,
        runner=runner,
        logger=logger,
        repo_dir=tmp_path,
    )


def _stub_branches(sm: ScrumMaster, branches: list[str]) -> None:
    """Configure runner to return the given branch list from git branch --list."""
    sm.runner.run.return_value = MagicMock(stdout="\n".join(branches) + "\n")


# ---------------------------------------------------------------------------
# test_identifies_stale_branches_no_open_pr
# ---------------------------------------------------------------------------


class TestIdentifiesStalesBranchesNoOpenPr:
    def test_branch_with_no_open_pr_is_deleted(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        _stub_branches(sm, ["ralph/M1-01-some-feature"])
        sm.pr_manager.get_existing.return_value = None
        # age: fresh branch, no PR → stale because no open PR
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M1-01-some-feature\n"),  # git branch --list
            MagicMock(stdout="9999999999\n"),  # git log --format=%ct (very old timestamp)
        ]

        deleted = sm._post_sprint_cleanup()

        assert deleted == ["ralph/M1-01-some-feature"]
        sm.branch_manager.delete_local.assert_called_once_with(
            "ralph/M1-01-some-feature", ignore_missing=True
        )

    def test_returns_all_stale_branch_names(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        branches = ["ralph/M1-01-feat-a", "ralph/M1-02-feat-b"]
        sm.runner.run.side_effect = [
            MagicMock(stdout="\n".join(branches) + "\n"),
            MagicMock(stdout="9999999999\n"),  # age for first branch
            MagicMock(stdout="9999999999\n"),  # age for second branch
        ]
        sm.pr_manager.get_existing.return_value = None

        deleted = sm._post_sprint_cleanup()

        assert set(deleted) == set(branches)

    def test_no_ralph_branches_returns_empty(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.return_value = MagicMock(stdout="")
        sm.pr_manager.get_existing.return_value = None

        deleted = sm._post_sprint_cleanup()

        assert deleted == []
        sm.branch_manager.delete_local.assert_not_called()


# ---------------------------------------------------------------------------
# test_deletes_branches_older_than_seven_days
# ---------------------------------------------------------------------------


class TestDeletesBranchesOlderThanSevenDays:
    def test_branch_older_than_seven_days_is_deleted(self, tmp_path):
        import time

        sm = _make_scrum_master(tmp_path)
        eight_days_ago = int(time.time()) - (8 * 86400)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M2-01-old-branch\n"),
            MagicMock(stdout=f"{eight_days_ago}\n"),
        ]
        sm.pr_manager.get_existing.return_value = None

        deleted = sm._post_sprint_cleanup()

        assert "ralph/M2-01-old-branch" in deleted
        sm.branch_manager.delete_local.assert_called_once_with(
            "ralph/M2-01-old-branch", ignore_missing=True
        )

    def test_branch_exactly_at_boundary_is_deleted(self, tmp_path):
        """A branch with age > STALE_DAYS days is stale."""
        import time

        sm = _make_scrum_master(tmp_path)
        eight_days_one_second_ago = int(time.time()) - (8 * 86400 + 1)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M2-02-boundary\n"),
            MagicMock(stdout=f"{eight_days_one_second_ago}\n"),
        ]
        sm.pr_manager.get_existing.return_value = None

        deleted = sm._post_sprint_cleanup()

        assert "ralph/M2-02-boundary" in deleted

    def test_branch_with_empty_log_treated_as_infinitely_old(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M2-03-empty-log\n"),
            MagicMock(stdout=""),  # no commits
        ]
        sm.pr_manager.get_existing.return_value = None

        deleted = sm._post_sprint_cleanup()

        assert "ralph/M2-03-empty-log" in deleted


# ---------------------------------------------------------------------------
# test_skips_branches_with_open_prs
# ---------------------------------------------------------------------------


class TestSkipsBranchesWithOpenPrs:
    def test_branch_with_open_pr_is_not_deleted(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.return_value = MagicMock(stdout="ralph/M3-01-active\n")
        sm.pr_manager.get_existing.return_value = PRInfo(
            number=42, url="https://github.com/org/repo/pull/42"
        )

        deleted = sm._post_sprint_cleanup()

        assert deleted == []
        sm.branch_manager.delete_local.assert_not_called()

    def test_mixed_branches_only_deletes_stale(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M3-01-active\nralph/M3-02-stale\n"),
            MagicMock(stdout="9999999999\n"),  # age for M3-02-stale
        ]
        active_pr = PRInfo(number=7, url="https://github.com/org/repo/pull/7")
        sm.pr_manager.get_existing.side_effect = [active_pr, None]

        deleted = sm._post_sprint_cleanup()

        assert deleted == ["ralph/M3-02-stale"]
        sm.branch_manager.delete_local.assert_called_once_with(
            "ralph/M3-02-stale", ignore_missing=True
        )

    def test_all_branches_with_open_prs_none_deleted(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.return_value = MagicMock(
            stdout="ralph/M3-01-a\nralph/M3-02-b\nralph/M3-03-c\n"
        )
        sm.pr_manager.get_existing.return_value = PRInfo(
            number=1, url="https://github.com/org/repo/pull/1"
        )

        deleted = sm._post_sprint_cleanup()

        assert deleted == []
        sm.branch_manager.delete_local.assert_not_called()


# ---------------------------------------------------------------------------
# test_logs_all_deleted_branches
# ---------------------------------------------------------------------------


class TestLogsAllDeletedBranches:
    def test_logs_each_deleted_branch(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M4-01-del-a\nralph/M4-02-del-b\n"),
            MagicMock(stdout="9999999999\n"),
            MagicMock(stdout="9999999999\n"),
        ]
        sm.pr_manager.get_existing.return_value = None

        sm._post_sprint_cleanup()

        info_calls = [str(c) for c in sm.logger.info.call_args_list]
        assert any("ralph/M4-01-del-a" in c for c in info_calls)
        assert any("ralph/M4-02-del-b" in c for c in info_calls)

    def test_logs_skipped_branch_with_open_pr(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.return_value = MagicMock(stdout="ralph/M4-03-active\n")
        sm.pr_manager.get_existing.return_value = PRInfo(
            number=99, url="https://github.com/org/repo/pull/99"
        )

        sm._post_sprint_cleanup()

        info_calls = [str(c) for c in sm.logger.info.call_args_list]
        assert any("ralph/M4-03-active" in c for c in info_calls)
        # Deletion must not be logged
        assert not any("Deleting" in c and "ralph/M4-03-active" in c for c in info_calls)

    def test_logs_cleanup_summary(self, tmp_path):
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M4-04-x\n"),
            MagicMock(stdout="9999999999\n"),
        ]
        sm.pr_manager.get_existing.return_value = None

        sm._post_sprint_cleanup()

        info_calls = [str(c) for c in sm.logger.info.call_args_list]
        assert any("Cleanup complete" in c for c in info_calls)

    def test_delete_local_called_with_ignore_missing(self, tmp_path):
        """BranchManager.delete_local is always called with ignore_missing=True."""
        sm = _make_scrum_master(tmp_path)
        sm.runner.run.side_effect = [
            MagicMock(stdout="ralph/M4-05-y\n"),
            MagicMock(stdout="9999999999\n"),
        ]
        sm.pr_manager.get_existing.return_value = None

        sm._post_sprint_cleanup()

        sm.branch_manager.delete_local.assert_called_once_with("ralph/M4-05-y", ignore_missing=True)
