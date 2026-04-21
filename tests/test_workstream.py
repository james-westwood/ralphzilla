"""Tests for M5-05 workstream_namespacing.

Covers:
- --workstream flag filters tasks by prefix in prd.json
- Branch names include workstream prefix when flag set
- Worktree paths include workstream directory
- Without --workstream, all tasks selected and no prefix added
- Multiple workstreams can run concurrently with isolated branches
- TaskTracker.load_tasks() respects workstream filter
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from ralph import TaskTracker, WorktreeManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tracker(
    tmp_path: Path,
    tasks: list[dict],
    workstream: str | None = None,
) -> TaskTracker:
    prd_file = tmp_path / "prd.json"
    prd_file.write_text(json.dumps({"tasks": tasks}))
    return TaskTracker(
        prd_file,
        tmp_path / "progress.txt",
        MagicMock(),
        MagicMock(),
        workstream=workstream,
    )


def make_worktree_manager(
    repo_dir: Path,
    workstream: str | None = None,
) -> WorktreeManager:
    runner = MagicMock()
    runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    return WorktreeManager(repo_dir, runner, MagicMock(), workstream=workstream)


# ---------------------------------------------------------------------------
# TaskTracker.load_tasks() — workstream filter
# ---------------------------------------------------------------------------


class TestLoadTasks:
    def test_no_workstream_returns_all_tasks(self, tmp_path):
        tasks = [
            {"id": "M5-01", "title": "A"},
            {"id": "M6-01", "title": "B"},
            {"id": "M5-02", "title": "C"},
        ]
        tracker = make_tracker(tmp_path, tasks)
        result = tracker.load_tasks()
        assert len(result) == 3

    def test_workstream_on_instance_filters_by_prefix(self, tmp_path):
        tasks = [
            {"id": "M5-01", "title": "A"},
            {"id": "M6-01", "title": "B"},
            {"id": "M5-02", "title": "C"},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        result = tracker.load_tasks()
        assert all(t["id"].startswith("M5") for t in result)
        assert len(result) == 2

    def test_explicit_workstream_arg_overrides_instance(self, tmp_path):
        tasks = [
            {"id": "M5-01", "title": "A"},
            {"id": "M6-01", "title": "B"},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        # Pass M6 explicitly — should override self.workstream
        result = tracker.load_tasks(workstream="M6")
        assert len(result) == 1
        assert result[0]["id"] == "M6-01"

    def test_explicit_none_arg_falls_back_to_instance_workstream(self, tmp_path):
        tasks = [
            {"id": "M5-01", "title": "A"},
            {"id": "M6-01", "title": "B"},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        # Passing workstream=None falls back to self.workstream ("M5")
        result = tracker.load_tasks(workstream=None)
        assert len(result) == 1
        assert result[0]["id"] == "M5-01"

    def test_returns_empty_list_when_no_tasks_match(self, tmp_path):
        tasks = [{"id": "M6-01", "title": "B"}]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        assert tracker.load_tasks() == []


# ---------------------------------------------------------------------------
# TaskTracker.get_next_task() — workstream filter
# ---------------------------------------------------------------------------


class TestGetNextTaskWorkstream:
    def test_workstream_filters_eligible_tasks(self, tmp_path):
        tasks = [
            {"id": "M5-01", "owner": "ralph", "completed": False},
            {"id": "M6-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        task = tracker.get_next_task()
        assert task is not None
        assert task["id"] == "M5-01"

    def test_workstream_skips_tasks_from_other_streams(self, tmp_path):
        tasks = [
            {"id": "M6-01", "owner": "ralph", "completed": False},
            {"id": "M5-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        task = tracker.get_next_task()
        assert task is not None
        assert task["id"] == "M5-01"

    def test_without_workstream_all_tasks_eligible(self, tmp_path):
        tasks = [
            {"id": "M6-01", "owner": "ralph", "completed": False},
            {"id": "M5-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks)  # no workstream
        task = tracker.get_next_task()
        assert task is not None
        assert task["id"] == "M6-01"  # first in list

    def test_returns_none_when_no_matching_incomplete_tasks(self, tmp_path):
        tasks = [
            {"id": "M6-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        assert tracker.get_next_task() is None

    def test_completed_ids_span_all_tasks_for_dependency_resolution(self, tmp_path):
        """A workstream task depending on a different-workstream task resolves correctly."""
        tasks = [
            {"id": "M4-01", "owner": "ralph", "completed": True},
            {
                "id": "M5-01",
                "owner": "ralph",
                "completed": False,
                "depends_on": ["M4-01"],
            },
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        task = tracker.get_next_task()
        assert task is not None
        assert task["id"] == "M5-01"


# ---------------------------------------------------------------------------
# TaskTracker.count_remaining() — workstream filter
# ---------------------------------------------------------------------------


class TestCountRemainingWorkstream:
    def test_counts_only_workstream_tasks(self, tmp_path):
        tasks = [
            {"id": "M5-01", "owner": "ralph", "completed": False},
            {"id": "M5-02", "owner": "ralph", "completed": False},
            {"id": "M6-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks, workstream="M5")
        assert tracker.count_remaining() == 2

    def test_without_workstream_counts_all(self, tmp_path):
        tasks = [
            {"id": "M5-01", "owner": "ralph", "completed": False},
            {"id": "M6-01", "owner": "ralph", "completed": False},
        ]
        tracker = make_tracker(tmp_path, tasks)
        assert tracker.count_remaining() == 2


# ---------------------------------------------------------------------------
# WorktreeManager — branch names with workstream
# ---------------------------------------------------------------------------


class TestBranchNamingWithWorkstream:
    def test_workstream_prefix_in_branch_name(self):
        mgr = make_worktree_manager(Path("/repo"), workstream="M5")
        assert mgr._branch_name("M5-01") == "feature-M5-M5-01"

    def test_no_workstream_no_prefix(self):
        mgr = make_worktree_manager(Path("/repo"))
        assert mgr._branch_name("M5-01") == "feature-M5-01"

    def test_different_workstreams_produce_distinct_branches(self):
        mgr_a = make_worktree_manager(Path("/repo"), workstream="auth")
        mgr_b = make_worktree_manager(Path("/repo"), workstream="billing")
        assert mgr_a._branch_name("T1") != mgr_b._branch_name("T1")

    def test_same_task_different_workstreams_do_not_collide(self):
        mgr_a = make_worktree_manager(Path("/repo"), workstream="M5")
        mgr_b = make_worktree_manager(Path("/repo"), workstream="M6")
        assert mgr_a._branch_name("task-01") != mgr_b._branch_name("task-01")


# ---------------------------------------------------------------------------
# WorktreeManager — worktree paths include workstream directory
# ---------------------------------------------------------------------------


class TestWorktreePathWithWorkstream:
    def test_workstream_adds_subdirectory(self):
        mgr = make_worktree_manager(Path("/repo"), workstream="M5")
        path = mgr._worktree_path("M5-01")
        assert path == Path("/repo/.ralph/worktrees/M5/M5-01")

    def test_no_workstream_path_unchanged(self):
        mgr = make_worktree_manager(Path("/repo"))
        path = mgr._worktree_path("M5-01")
        assert path == Path("/repo/.ralph/worktrees/M5-01")

    def test_different_workstreams_have_isolated_paths(self):
        mgr_a = make_worktree_manager(Path("/repo"), workstream="auth")
        mgr_b = make_worktree_manager(Path("/repo"), workstream="billing")
        assert mgr_a._worktree_path("T1") != mgr_b._worktree_path("T1")

    def test_worktree_path_is_under_ralph_worktrees(self):
        mgr = make_worktree_manager(Path("/repo"), workstream="M5")
        path = mgr._worktree_path("task-01")
        assert ".ralph/worktrees" in str(path)


# ---------------------------------------------------------------------------
# WorktreeManager — create_worktree uses workstream subdir
# ---------------------------------------------------------------------------


class TestCreateWorktreeWithWorkstream:
    def test_create_worktree_returns_workstream_scoped_path(self, tmp_path):
        mgr = make_worktree_manager(tmp_path, workstream="M5")
        path = mgr.create_worktree("M5-01", "main")
        assert path == tmp_path / ".ralph/worktrees/M5/M5-01"

    def test_create_worktree_uses_workstream_branch_name(self, tmp_path):
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = WorktreeManager(tmp_path, runner, MagicMock(), workstream="M5")
        mgr.create_worktree("M5-01", "main")

        cmd = runner.run.call_args_list[0][0][0]
        branch_idx = cmd.index("-b") + 1
        assert cmd[branch_idx] == "feature-M5-M5-01"

    def test_parent_dir_created_under_workstream_subdir(self, tmp_path):
        mgr = make_worktree_manager(tmp_path, workstream="M5")
        mgr.create_worktree("M5-01", "main")
        assert (tmp_path / ".ralph/worktrees/M5").exists()


# ---------------------------------------------------------------------------
# WorktreeManager — list_active_worktrees scoped to workstream
# ---------------------------------------------------------------------------


class TestListActiveWorktreesWithWorkstream:
    def test_lists_only_workstream_directory(self, tmp_path):
        mgr = make_worktree_manager(tmp_path, workstream="M5")
        base = tmp_path / ".ralph/worktrees/M5"
        (base / "M5-01").mkdir(parents=True)
        (base / "M5-02").mkdir(parents=True)
        # A task from another workstream should not appear
        (tmp_path / ".ralph/worktrees/M6/M6-01").mkdir(parents=True)

        result = mgr.list_active_worktrees()
        assert sorted(result) == ["M5-01", "M5-02"]

    def test_no_workstream_lists_top_level_dirs(self, tmp_path):
        mgr = make_worktree_manager(tmp_path)
        base = tmp_path / ".ralph/worktrees"
        (base / "task-A").mkdir(parents=True)
        (base / "task-B").mkdir(parents=True)

        result = mgr.list_active_worktrees()
        assert sorted(result) == ["task-A", "task-B"]

    def test_returns_empty_when_workstream_dir_missing(self, tmp_path):
        mgr = make_worktree_manager(tmp_path, workstream="M5")
        assert mgr.list_active_worktrees() == []


# ---------------------------------------------------------------------------
# Concurrent workstreams — isolation guarantees
# ---------------------------------------------------------------------------


class TestConcurrentWorkstreams:
    def test_two_workstreams_produce_disjoint_branch_names(self):
        mgr_m5 = make_worktree_manager(Path("/repo"), workstream="M5")
        mgr_m6 = make_worktree_manager(Path("/repo"), workstream="M6")

        branches_m5 = {mgr_m5._branch_name(f"task-{i}") for i in range(3)}
        branches_m6 = {mgr_m6._branch_name(f"task-{i}") for i in range(3)}
        assert branches_m5.isdisjoint(branches_m6)

    def test_two_workstreams_produce_disjoint_worktree_paths(self):
        mgr_m5 = make_worktree_manager(Path("/repo"), workstream="M5")
        mgr_m6 = make_worktree_manager(Path("/repo"), workstream="M6")

        paths_m5 = {mgr_m5._worktree_path(f"task-{i}") for i in range(3)}
        paths_m6 = {mgr_m6._worktree_path(f"task-{i}") for i in range(3)}
        assert paths_m5.isdisjoint(paths_m6)

    def test_workstream_task_selection_is_independent(self, tmp_path):
        tasks = [
            {"id": "M5-01", "owner": "ralph", "completed": False},
            {"id": "M6-01", "owner": "ralph", "completed": False},
        ]
        tracker_m5 = make_tracker(tmp_path, tasks, workstream="M5")
        tracker_m6 = make_tracker(tmp_path, tasks, workstream="M6")

        task_m5 = tracker_m5.get_next_task()
        task_m6 = tracker_m6.get_next_task()

        assert task_m5["id"] == "M5-01"
        assert task_m6["id"] == "M6-01"
        assert task_m5["id"] != task_m6["id"]
