import json
from unittest.mock import MagicMock

import pytest

from ralph import PRDGuardViolation, TaskTracker


class TestTaskTrackerFreshLoad:
    def test_fresh_load_reads_disk_each_call(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_data = {"tasks": [{"id": "T1", "completed": False}]}
        prd_file.write_text(json.dumps(prd_data))

        tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

        result1 = tracker.load()
        result1["tasks"][0]["completed"] = True

        result2 = tracker.load()
        assert result2["tasks"][0]["completed"] is False


class TestTaskTrackerGetNextTask:
    def test_get_next_task_returns_incomplete_ralph_owned_task(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_data = {
            "tasks": [
                {"id": "T1", "owner": "ralph", "completed": False},
                {"id": "T2", "owner": "ralph", "completed": False},
            ]
        }
        prd_file.write_text(json.dumps(prd_data))
        tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

        task = tracker.get_next_task()
        assert task is not None
        assert task["owner"] == "ralph"
        assert task["completed"] is False
        assert task["id"] == "T1"

    def test_get_next_task_skips_human_owned_tasks(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_data = {
            "tasks": [
                {"id": "T1", "owner": "human", "completed": False},
                {"id": "T2", "owner": "ralph", "completed": False},
            ]
        }
        prd_file.write_text(json.dumps(prd_data))
        tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

        task = tracker.get_next_task()
        assert task is not None
        assert task["id"] == "T2"
        assert task["owner"] == "ralph"

    def test_get_next_task_respects_depends_on_completion(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_data = {
            "tasks": [
                {"id": "T1", "owner": "ralph", "completed": False},
                {"id": "T2", "owner": "ralph", "completed": False, "depends_on": ["T1"]},
                {"id": "T3", "owner": "ralph", "completed": False, "depends_on": ["T1", "T2"]},
            ]
        }
        prd_file.write_text(json.dumps(prd_data))
        tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

        task1 = tracker.get_next_task()
        assert task1["id"] == "T1"

        tracker.mark_complete("T1")

        task2 = tracker.get_next_task()
        assert task2["id"] == "T2"

        tracker.mark_complete("T2")

        task3 = tracker.get_next_task()
        assert task3["id"] == "T3"


class TestTaskTrackerMarkComplete:
    def test_mark_complete_writes_back_to_disk(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_data = {"tasks": [{"id": "T1", "completed": False, "owner": "ralph"}]}
        prd_file.write_text(json.dumps(prd_data))
        tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

        tracker.mark_complete("T1")

        loaded = tracker.load()
        assert loaded["tasks"][0]["completed"] is True


class TestTaskTrackerCommitTracking:
    def test_commit_tracking_issues_correct_git_commands(self, tmp_path):
        runner = MagicMock()
        prd_file = tmp_path / "prd.json"
        progress_file = tmp_path / "progress.txt"
        prd_file.write_text("{}")
        progress_file.write_text("")

        tracker = TaskTracker(prd_file, progress_file, runner, MagicMock())

        tracker.commit_tracking("T1", "Test Task")

        assert runner.run.call_count == 3

        calls = runner.run.call_args_list

        assert calls[0][0][0] == ["git", "add", str(prd_file), str(progress_file)]
        assert calls[0][1]["check"] is True

        assert calls[1][0][0] == ["git", "commit", "-m", "[T1] Test Task: mark complete"]
        assert calls[1][1]["check"] is True

        assert calls[2][0][0] == ["git", "push", "origin", "main"]
        assert calls[2][1]["check"] is True


def test_task_tracker_load(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": [{"id": "T1", "completed": False}]}
    prd_file.write_text(json.dumps(prd_data))

    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())
    assert tracker.load() == prd_data


def test_get_next_task(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {
        "tasks": [
            {"id": "T1", "owner": "ralph", "completed": True},
            {"id": "T2", "owner": "human", "completed": False},
            {"id": "T3", "owner": "ralph", "completed": False, "depends_on": ["T4"]},
            {"id": "T4", "owner": "ralph", "completed": False},
            {"id": "T5", "owner": "ralph", "completed": False, "depends_on": ["T1"]},
        ]
    }
    prd_file.write_text(json.dumps(prd_data))
    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

    task = tracker.get_next_task()
    assert task["id"] == "T4"


def test_mark_complete(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": [{"id": "T1", "completed": False}]}
    prd_file.write_text(json.dumps(prd_data))
    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

    tracker.mark_complete("T1")
    assert tracker.load()["tasks"][0]["completed"] is True


def test_mark_complete_already_done(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": [{"id": "T1", "completed": True}]}
    prd_file.write_text(json.dumps(prd_data))
    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

    with pytest.raises(PRDGuardViolation):
        tracker.mark_complete("T1")


def test_append_progress_writes_human_readable_format(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": [{"id": "M1-01", "epic": "M1", "title": "Test task", "completed": True}]}
    prd_file.write_text(json.dumps(prd_data))

    progress_file = tmp_path / "progress.txt"
    tracker = TaskTracker(prd_file, progress_file, MagicMock(), MagicMock())

    tracker.append_progress(
        "M1-01", "Test task", 123, "2026-04-20", sprint_start_date="2026-04-20", iteration_count=1
    )

    content = progress_file.read_text()
    assert "| Epic | Task ID | Title | Status | Completed | PR |" in content
    assert "| M1 | M1-01 | Test task" in content
    assert "✓" in content
    assert "#123" in content
    assert "Sprint Start" in content
    assert "Iteration" in content


def test_symbols_correct_for_completed_tasks(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {
        "tasks": [
            {"id": "T1", "epic": "M1", "title": "Done", "completed": True},
            {"id": "T2", "epic": "M1", "title": "Escalated", "completed": True, "escalated": True},
            {"id": "T3", "epic": "M1", "title": "Pending", "completed": False},
        ]
    }
    prd_file.write_text(json.dumps(prd_data))

    progress_file = tmp_path / "progress.txt"
    tracker = TaskTracker(prd_file, progress_file, MagicMock(), MagicMock())

    tracker.append_progress(
        "T3", "Pending", 0, "2026-04-20", sprint_start_date="2026-04-20", iteration_count=1
    )

    content = progress_file.read_text()
    assert content.count("✓") == 1
    assert content.count("⚠") == 1
    assert content.count("⏸") == 1


def test_header_includes_sprint_metadata(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": []}
    prd_file.write_text(json.dumps(prd_data))

    progress_file = tmp_path / "progress.txt"
    tracker = TaskTracker(prd_file, progress_file, MagicMock(), MagicMock())

    tracker.append_progress(
        "T1", "Title", 1, "2026-04-21", sprint_start_date="2026-04-20", iteration_count=5
    )

    content = progress_file.read_text()
    assert "**Sprint Start**: 2026-04-20" in content
    assert "**Iteration**: 5" in content


def test_add_task(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": []}
    prd_file.write_text(json.dumps(prd_data))
    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

    new_task = {"id": "T2", "title": "New Task"}
    tracker.add_task(new_task)

    loaded = tracker.load()
    assert len(loaded["tasks"]) == 1
    assert loaded["tasks"][0]["id"] == "T2"


def test_mark_decomposed(tmp_path):
    prd_file = tmp_path / "prd.json"
    prd_data = {"tasks": [{"id": "T1", "decomposed": False}]}
    prd_file.write_text(json.dumps(prd_data))
    tracker = TaskTracker(prd_file, tmp_path / "progress.txt", MagicMock(), MagicMock())

    tracker.mark_decomposed("T1")
    assert tracker.load()["tasks"][0]["decomposed"] is True
