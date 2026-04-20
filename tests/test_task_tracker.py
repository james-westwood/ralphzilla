import json
from unittest.mock import MagicMock

from ralph import PRDGuardViolation, TaskTracker


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

    # T1 is completed.
    # T2 is human.
    # T3 depends on T4 (incomplete).
    # T4 is next available ralph task.
    # T5 depends on T1 (completed), so it would be next after T4.

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

    import pytest

    with pytest.raises(PRDGuardViolation):
        tracker.mark_complete("T1")


def test_append_progress(tmp_path):
    progress_file = tmp_path / "progress.txt"
    tracker = TaskTracker(tmp_path / "prd.json", progress_file, MagicMock(), MagicMock())

    tracker.append_progress("T1", "Title", 123, "2026-04-20")
    assert progress_file.read_text() == "2026-04-20 | T1 | Title | PR #123\n"


def test_commit_tracking(tmp_path):
    runner = MagicMock()
    prd_file = tmp_path / "prd.json"
    progress_file = tmp_path / "progress.txt"
    tracker = TaskTracker(prd_file, progress_file, runner, MagicMock())

    tracker.commit_tracking("T1", "Title")

    assert runner.run.call_count == 3
    # Check git add
    runner.run.assert_any_call(["git", "add", str(prd_file), str(progress_file)], check=True)


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
