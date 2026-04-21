"""Tests for WorktreeManager — git worktree-based branch isolation."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ralph import TaskResult, WorktreeError, WorktreeManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_manager(
    repo_dir: Path = Path("/repo"),
    workstream: str | None = None,
) -> tuple[WorktreeManager, MagicMock]:
    runner = MagicMock()
    logger = MagicMock()
    runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mgr = WorktreeManager(repo_dir, runner, logger, workstream=workstream)
    return mgr, runner


# ---------------------------------------------------------------------------
# Branch naming
# ---------------------------------------------------------------------------


class TestBranchNaming:
    def test_no_workstream_uses_feature_prefix(self):
        mgr, _ = make_manager()
        assert mgr._branch_name("M5-03") == "feature-M5-03"

    def test_workstream_included_in_branch_name(self):
        mgr, _ = make_manager(workstream="auth")
        assert mgr._branch_name("M5-03") == "feature-auth-M5-03"

    def test_workstream_none_omits_prefix(self):
        mgr, _ = make_manager(workstream=None)
        assert mgr._branch_name("task-01") == "feature-task-01"

    def test_different_workstreams_produce_distinct_names(self):
        mgr_a, _ = make_manager(workstream="billing")
        mgr_b, _ = make_manager(workstream="infra")
        assert mgr_a._branch_name("T1") != mgr_b._branch_name("T1")


# ---------------------------------------------------------------------------
# Worktree path
# ---------------------------------------------------------------------------


class TestWorktreePath:
    def test_path_is_under_ralph_worktrees(self):
        mgr, _ = make_manager(repo_dir=Path("/repo"))
        path = mgr._worktree_path("my-task")
        assert path == Path("/repo/.ralph/worktrees/my-task")

    def test_different_tasks_get_different_paths(self):
        mgr, _ = make_manager()
        assert mgr._worktree_path("A") != mgr._worktree_path("B")


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    def test_creates_isolated_git_worktree_directory(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        path = mgr.create_worktree("task-01", "main")

        assert path == tmp_path / ".ralph/worktrees/task-01"

        # git worktree add must have been called
        git_calls = [c[0][0] for c in runner.run.call_args_list]
        worktree_add = [c for c in git_calls if "worktree" in c and "add" in c]
        assert len(worktree_add) == 1

    def test_worktree_add_uses_correct_branch_and_base(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        mgr.create_worktree("task-01", "main")

        cmd = runner.run.call_args_list[0][0][0]
        assert cmd[0] == "git"
        assert "worktree" in cmd
        assert "add" in cmd
        assert "-b" in cmd
        assert "feature-task-01" in cmd
        assert "main" in cmd

    def test_workstream_prefix_in_branch_name(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path, workstream="payments")
        mgr.create_worktree("task-02", "main")

        cmd = runner.run.call_args_list[0][0][0]
        branch_idx = cmd.index("-b") + 1
        assert cmd[branch_idx] == "feature-payments-task-02"

    def test_parent_directory_created_before_git_call(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        # Ensure the parent worktrees directory does not pre-exist
        worktrees_dir = tmp_path / ".ralph/worktrees"
        assert not worktrees_dir.exists()

        mgr.create_worktree("task-01", "main")
        assert worktrees_dir.exists()

    def test_raises_worktree_error_on_git_failure(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        runner.run.return_value = MagicMock(returncode=1, stdout="", stderr="cannot lock ref")

        with pytest.raises(WorktreeError, match="task-01"):
            mgr.create_worktree("task-01", "main")

    def test_cwd_passed_to_runner(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        mgr.create_worktree("task-01", "main")
        _, kwargs = runner.run.call_args_list[0]
        assert (
            kwargs.get("cwd") == tmp_path or runner.run.call_args_list[0][1].get("cwd") == tmp_path
        )

    def test_parallel_tasks_get_separate_paths(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        path_a = mgr._worktree_path("A")
        path_b = mgr._worktree_path("B")
        assert path_a != path_b


# ---------------------------------------------------------------------------
# cleanup_worktree
# ---------------------------------------------------------------------------


class TestCleanupWorktree:
    def test_calls_git_worktree_remove(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        mgr.cleanup_worktree("task-01")

        cmds = [c[0][0] for c in runner.run.call_args_list]
        remove_cmds = [c for c in cmds if "worktree" in c and "remove" in c]
        assert len(remove_cmds) == 1

    def test_calls_git_worktree_prune(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        mgr.cleanup_worktree("task-01")

        cmds = [c[0][0] for c in runner.run.call_args_list]
        prune_cmds = [c for c in cmds if "worktree" in c and "prune" in c]
        assert len(prune_cmds) == 1

    def test_removes_leftover_directory(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        # Simulate a directory that git worktree remove did not delete
        leftover = tmp_path / ".ralph/worktrees/task-01"
        leftover.mkdir(parents=True)
        assert leftover.exists()

        # Mock runner so git worktree remove does NOT actually delete the dir
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        mgr.cleanup_worktree("task-01")
        assert not leftover.exists()

    def test_safe_to_call_when_directory_missing(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        # Should not raise even if the worktree directory was never created
        mgr.cleanup_worktree("nonexistent-task")

    def test_remove_called_with_force_flag(self, tmp_path):
        mgr, runner = make_manager(repo_dir=tmp_path)
        mgr.cleanup_worktree("task-01")

        cmds = [c[0][0] for c in runner.run.call_args_list]
        remove_cmd = next(c for c in cmds if "worktree" in c and "remove" in c)
        assert "--force" in remove_cmd


# ---------------------------------------------------------------------------
# list_active_worktrees
# ---------------------------------------------------------------------------


class TestListActiveWorktrees:
    def test_returns_empty_list_when_no_worktrees_dir(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        assert mgr.list_active_worktrees() == []

    def test_returns_task_ids_of_existing_directories(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        base = tmp_path / ".ralph/worktrees"
        (base / "task-A").mkdir(parents=True)
        (base / "task-B").mkdir(parents=True)

        result = mgr.list_active_worktrees()
        assert sorted(result) == ["task-A", "task-B"]

    def test_ignores_files_only_returns_directories(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        base = tmp_path / ".ralph/worktrees"
        base.mkdir(parents=True)
        (base / "task-A").mkdir()
        (base / "some-file.txt").write_text("x")

        result = mgr.list_active_worktrees()
        assert result == ["task-A"]

    def test_result_is_sorted(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        base = tmp_path / ".ralph/worktrees"
        for name in ["Z-task", "A-task", "M-task"]:
            (base / name).mkdir(parents=True)

        result = mgr.list_active_worktrees()
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# make_isolated_runner — integration: worktree lifecycle around task execution
# ---------------------------------------------------------------------------


class TestMakeIsolatedRunner:
    def test_worktree_created_before_inner_runner(self, tmp_path):
        call_order: list[str] = []

        def fake_create(task_id: str, base_branch: str) -> Path:
            call_order.append("create")
            return tmp_path / ".ralph/worktrees" / task_id

        def fake_cleanup(task_id: str) -> None:
            call_order.append("cleanup")

        mgr, _ = make_manager(repo_dir=tmp_path)
        mgr.create_worktree = fake_create  # type: ignore[method-assign]
        mgr.cleanup_worktree = fake_cleanup  # type: ignore[method-assign]

        def inner(task_id: str, worktree_path: Path) -> TaskResult:
            call_order.append("run")
            return TaskResult(fatal=False, message="ok")

        runner = mgr.make_isolated_runner(inner)
        runner("task-01")

        assert call_order == ["create", "run", "cleanup"]

    def test_worktree_path_passed_to_inner(self, tmp_path):
        received_path: list[Path] = []

        mgr, _ = make_manager(repo_dir=tmp_path)

        # Override create_worktree to return a predictable path without git
        expected = tmp_path / ".ralph/worktrees/task-01"

        def fake_create(task_id: str, base_branch: str) -> Path:
            return expected

        mgr.create_worktree = fake_create  # type: ignore[method-assign]
        mgr.cleanup_worktree = MagicMock()

        def inner(task_id: str, worktree_path: Path) -> TaskResult:
            received_path.append(worktree_path)
            return TaskResult(fatal=False, message="ok")

        runner = mgr.make_isolated_runner(inner)
        runner("task-01")

        assert received_path == [expected]

    def test_cleanup_called_on_success(self, tmp_path):
        mgr, _ = make_manager(repo_dir=tmp_path)
        mgr.create_worktree = MagicMock(return_value=tmp_path / "wt")
        cleanup = MagicMock()
        mgr.cleanup_worktree = cleanup

        runner = mgr.make_isolated_runner(lambda tid, path: TaskResult(fatal=False, message="ok"))
        runner("task-01")
        cleanup.assert_called_once_with("task-01")

    def test_cleanup_called_on_failure(self, tmp_path):
        """Worktree is cleaned up even when the inner runner raises."""
        mgr, _ = make_manager(repo_dir=tmp_path)
        mgr.create_worktree = MagicMock(return_value=tmp_path / "wt")
        cleanup = MagicMock()
        mgr.cleanup_worktree = cleanup

        def failing_inner(task_id: str, path: Path) -> TaskResult:
            raise RuntimeError("coder exploded")

        runner = mgr.make_isolated_runner(failing_inner)
        with pytest.raises(RuntimeError):
            runner("task-01")

        cleanup.assert_called_once_with("task-01")

    def test_parallel_tasks_use_separate_worktrees(self, tmp_path):
        """Each task_id results in a distinct worktree path."""
        created_paths: list[Path] = []

        mgr, _ = make_manager(repo_dir=tmp_path)

        def tracking_create(self_inner: WorktreeManager, task_id: str, base_branch: str) -> Path:
            path = self_inner._worktree_path(task_id)
            created_paths.append(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir(exist_ok=True)
            return path

        mgr.create_worktree = lambda tid, base: tracking_create(mgr, tid, base)
        mgr.cleanup_worktree = MagicMock()

        results = {}
        for tid in ["A", "B", "C"]:
            runner = mgr.make_isolated_runner(
                lambda task_id, path: TaskResult(fatal=False, message=str(path))
            )
            results[tid] = runner(tid)

        # All created paths must be distinct
        assert len(set(created_paths)) == len(created_paths) == 3
        # Each result message references its own path
        assert len({r.message for r in results.values()}) == 3

    def test_task_execution_uses_worktree_as_working_directory(self, tmp_path):
        """The worktree path is passed as the working directory to the inner runner."""
        mgr, _ = make_manager(repo_dir=tmp_path)

        wt_path = tmp_path / ".ralph/worktrees/task-01"
        mgr.create_worktree = MagicMock(return_value=wt_path)
        mgr.cleanup_worktree = MagicMock()

        received: list[Path] = []

        def inner(task_id: str, worktree_path: Path) -> TaskResult:
            received.append(worktree_path)
            return TaskResult(fatal=False, message="ok")

        runner = mgr.make_isolated_runner(inner)
        runner("task-01")

        assert received[0] == wt_path
