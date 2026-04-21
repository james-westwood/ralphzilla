import json
import os
import time
from pathlib import Path

import pytest

from ralph import LoopSupervisor, RalphLogger


@pytest.fixture
def supervisor(tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    progress_path = Path(tmp_path) / "progress.txt"
    logger = RalphLogger(log_path)
    ralph_path = Path(__file__).parent.parent / "ralph.py"
    return LoopSupervisor(logger, log_path, progress_path, ralph_path)


def test_clean_exit_all_markers_present(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:05:00 [INFO ] Task M1-01: implementing feature
2026-04-21 10:10:00 [INFO ] Task M1-01: completed
2026-04-21 10:15:00 [INFO ] Sprint complete
2026-04-21 10:15:01 [INFO ] progress.txt updated
2026-04-21 10:15:02 [INFO ] Loop finished.
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()

    assert result.clean is True
    assert result.has_sprint_complete is True
    assert result.has_progress_update is True
    assert result.no_traceback is True
    assert result.missing_markers == []


def test_detects_missing_sprint_complete_marker(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:05:00 [INFO ] Task M1-01: implementing feature
2026-04-21 10:10:00 [INFO ] Task M1-01: completed
2026-04-21 10:15:00 [INFO ] progress.txt updated
2026-04-21 10:15:01 [INFO ] Loop finished.
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()

    assert result.clean is False
    assert result.has_sprint_complete is False
    assert result.has_progress_update is True
    assert "Sprint complete marker missing" in result.missing_markers


def test_detects_traceback_in_final_lines(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:05:00 [INFO ] Task M1-01: implementing feature
2026-04-21 10:10:00 [INFO ] Some error occurred
2026-04-21 10:10:05 [ERROR] Traceback (most recent call last):
2026-04-21 10:10:05 [ERROR]   File "ralph.py", line 100, in <module>
2026-04-21 10:10:05 [ERROR]     raise Exception("fatal")
2026-04-21 10:15:00 [INFO ] Sprint complete
2026-04-21 10:15:01 [INFO ] progress.txt updated
2026-04-21 10:15:02 [INFO ] Loop finished.
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()

    assert result.clean is False
    assert result.no_traceback is False
    assert result.fatal_error_type == "traceback_in_logs"
    assert "Traceback/Unhandled exception found in final log lines" in result.missing_markers


def test_detects_unhandled_exception_marker(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:15:00 [INFO ] Sprint complete
2026-04-21 10:15:01 [INFO ] progress.txt updated
2026-04-21 10:15:02 [INFO ] Unhandled exception in worker thread
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()

    assert result.clean is False
    assert result.no_traceback is False
    assert "Traceback/Unhandled exception found in final log lines" in result.missing_markers


def test_records_run_history_correctly(supervisor, tmp_path, capsys):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:15:00 [INFO ] Sprint complete
2026-04-21 10:15:01 [INFO ] progress.txt updated
2026-04-21 10:15:02 [INFO ] Loop finished.
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()
    supervisor.record_run(result, tasks_completed=3)

    history_path = Path(tmp_path) / ".ralph" / "run-history.json"
    assert history_path.exists()

    with open(history_path, "r") as f:
        history = json.load(f)

    assert len(history) == 1
    assert history[0]["tasks_completed"] == 3
    assert history[0]["final_state"] == "clean"
    assert history[0]["fatal_error_type"] is None
    assert "timestamp" in history[0]


def test_records_unclean_run_history(supervisor, tmp_path, capsys):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:15:00 [INFO ] Traceback occurred
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()
    supervisor.record_run(result, tasks_completed=1)

    history_path = Path(tmp_path) / ".ralph" / "run-history.json"

    with open(history_path, "r") as f:
        history = json.load(f)

    assert len(history) == 1
    assert history[0]["tasks_completed"] == 1
    assert history[0]["final_state"] == "unclean"
    assert history[0]["fatal_error_type"] == "traceback_in_logs"


def test_appends_to_existing_history(supervisor, tmp_path, capsys):
    history_path = Path(tmp_path) / ".ralph" / "run-history.json"
    history_path.parent.mkdir(parents=True)
    existing_history = [
        {
            "timestamp": "2026-04-20T10:00:00",
            "tasks_completed": 2,
            "final_state": "clean",
            "fatal_error_type": None,
        }
    ]
    history_path.write_text(json.dumps(existing_history))

    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:15:00 [INFO ] Sprint complete
2026-04-21 10:15:01 [INFO ] progress.txt updated
"""
    log_path.write_text(log_content)

    result = supervisor.verify_clean_exit()
    supervisor.record_run(result, tasks_completed=1)

    with open(history_path, "r") as f:
        history = json.load(f)

    assert len(history) == 2
    assert history[0]["tasks_completed"] == 2
    assert history[1]["tasks_completed"] == 1


def test_missing_log_file(supervisor, tmp_path, capsys):
    result = supervisor.verify_clean_exit()

    assert result.clean is False
    assert "ralph.log not found" in result.missing_markers
    assert result.fatal_error_type is None


def test_handles_empty_log_file(supervisor, tmp_path, capsys):
    log_path = Path(tmp_path) / "ralph.log"
    log_path.write_text("")

    result = supervisor.verify_clean_exit()

    assert result.clean is False
    assert result.has_sprint_complete is False
    assert result.has_progress_update is False
    assert result.no_traceback is True


def test_final_50_lines_only(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    lines = []
    for i in range(100):
        lines.append(f"2026-04-21 10:{i:02d}:00 [INFO ] Line {i}\n")
    lines.append("2026-04-21 10:50:00 [INFO ] Sprint complete\n")
    lines.append("2026-04-21 10:50:01 [INFO ] progress.txt updated\n")
    log_path.write_text("".join(lines))

    result = supervisor.verify_clean_exit()

    assert result.clean is True
    assert result.has_sprint_complete is True
    assert result.has_progress_update is True


def test_runs_ralph_as_subprocess_with_correct_args(supervisor, tmp_path, capsys):
    exit_code = supervisor.run(max_iterations=1, timeout=60)
    assert exit_code in (0, 1)


def test_captures_exit_code_on_completion(supervisor, tmp_path):
    exit_code = supervisor.run(max_iterations=1, timeout=60)
    assert isinstance(exit_code, int)
    assert exit_code == supervisor.get_exit_code()


def test_monitors_log_file_for_error_markers(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_content = """
2026-04-21 10:00:00 [INFO ] Starting sprint
2026-04-21 10:05:00 [ERROR] Something went wrong
2026-04-21 10:10:00 [INFO ] Sprint complete
"""
    log_path.write_text(log_content)

    errors = supervisor.parse_log_for_errors()
    assert len(errors) > 0
    assert any("ERROR" in e for e in errors)


def test_detects_hung_process_via_timeout(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_path.write_text("old log content")
    time.sleep(0.1)
    os.utime(log_path, (time.time() - 400, time.time() - 400))

    is_hung = supervisor.detect_hung(timeout=300)
    assert is_hung is True


def test_detects_healthy_process_not_hung(supervisor, tmp_path):
    log_path = Path(tmp_path) / "ralph.log"
    log_path.write_text("recent log content")

    is_hung = supervisor.detect_hung(timeout=300)
    assert is_hung is False
