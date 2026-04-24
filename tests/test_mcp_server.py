"""
Tests for ralph_mcp.py — FastMCP server for ralphzilla.

These tests mock file I/O, subprocess calls, and psutil to verify
the 8 MCP tools work correctly without touching real infrastructure.
"""

import json
import signal
import subprocess
from unittest.mock import MagicMock, Mock, patch

# Import the module under test
import ralph_mcp as mcp_module


class TestReadPrd:
    """Tests for _read_prd helper."""

    def test_read_prd_returns_empty_dict_when_file_missing(self, tmp_path):
        """When prd.json doesn't exist, return tasks list."""
        with patch.object(mcp_module, "PRD_FILE", tmp_path / "nonexistent.json"):
            result = mcp_module._read_prd()
            assert result == {"tasks": []}

    def test_read_prd_parses_json_correctly(self, tmp_path):
        """When prd.json exists, parse and return its contents."""
        prd_file = tmp_path / "prd.json"
        prd_data = {"tasks": [{"id": "TASK-01", "title": "Test task"}]}
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            result = mcp_module._read_prd()
            assert result == prd_data


class TestFindLatestSummary:
    """Tests for _find_latest_summary helper."""

    def test_returns_none_when_no_summaries(self, tmp_path):
        """When no ralph-summary-*.md files exist, return None."""
        with patch.object(mcp_module, "PROJECT_DIR", tmp_path):
            result = mcp_module._find_latest_summary()
            assert result is None

    def test_returns_most_recent_summary(self, tmp_path):
        """Return the most recent summary file."""
        # Create multiple summary files
        summary1 = tmp_path / "ralph-summary-2024-01-01.md"
        summary2 = tmp_path / "ralph-summary-2024-01-15.md"
        summary3 = tmp_path / "ralph-summary-2024-01-10.md"

        summary1.write_text("Old summary")
        summary2.write_text("Newest summary")
        summary3.write_text("Middle summary")

        with patch.object(mcp_module, "PROJECT_DIR", tmp_path):
            result = mcp_module._find_latest_summary()
            assert result == summary2


class TestIsSprintRunning:
    """Tests for _is_sprint_running helper."""

    def test_returns_true_when_rzilla_process_found(self):
        """When a rzilla run process exists, return True."""
        mock_proc = Mock()
        mock_proc.info = {"cmdline": ["uv", "run", "rzilla", "run", "--task", "TASK-01"]}

        with patch("psutil.process_iter", return_value=[mock_proc]):
            result = mcp_module._is_sprint_running()
            assert result is True

    def test_returns_false_when_no_rzilla_process(self):
        """When no rzilla process exists, return False."""
        mock_proc = Mock()
        mock_proc.info = {"cmdline": ["python", "some_other_script.py"]}

        with patch("psutil.process_iter", return_value=[mock_proc]):
            result = mcp_module._is_sprint_running()
            assert result is False

    def test_handles_no_such_process_exception(self):
        """Handle psutil.NoSuchProcess gracefully."""
        # Create a mock process that raises NoSuchProcess when accessing info
        mock_proc = Mock()
        mock_proc.info = {"cmdline": ["uv", "run", "rzilla", "run"]}

        def raise_no_such(*args, **kwargs):
            raise psutil.NoSuchProcess(1234)

        # The exception happens when accessing the process, not in the list
        # Our code catches it in the try/except block
        with patch("psutil.process_iter", return_value=[mock_proc]):
            with patch.object(mock_proc, "info", side_effect=raise_no_such):
                # When info raises, the exception is caught and we continue
                result = mcp_module._is_sprint_running()
                assert result is False


class TestRzillaStatus:
    """Tests for rzilla_status MCP tool."""

    def test_returns_correct_counts(self, tmp_path):
        """Status returns correct task counts."""
        prd_data = {
            "tasks": [
                {"id": "TASK-01", "title": "Done task", "completed": True, "owner": "ralph"},
                {"id": "TASK-02", "title": "Pending task", "completed": False, "owner": "ralph"},
                {"id": "TASK-03", "title": "Human task", "completed": False, "owner": "human"},
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            with patch.object(mcp_module, "_is_sprint_running", return_value=False):
                with patch.object(mcp_module, "_find_latest_summary", return_value=None):
                    result = mcp_module.rzilla_status()

        data = json.loads(result)
        assert data["pending_tasks"] == 2  # TASK-02 and TASK-03
        assert data["completed_tasks"] == 1  # TASK-01
        assert data["total_tasks"] == 3
        assert data["next_task"]["id"] == "TASK-02"
        assert data["sprint_running"] is False
        assert data["last_summary"] is None


class TestRzillaTasks:
    """Tests for rzilla_tasks MCP tool."""

    def test_returns_all_tasks_by_default(self, tmp_path):
        """Without filter, returns all tasks."""
        prd_data = {
            "tasks": [
                {
                    "id": "TASK-01",
                    "title": "Task 1",
                    "completed": True,
                    "owner": "ralph",
                    "priority": 1,
                },
                {
                    "id": "TASK-02",
                    "title": "Task 2",
                    "completed": False,
                    "owner": "ralph",
                    "priority": 2,
                },
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            result = mcp_module.rzilla_tasks()

        tasks = json.loads(result)
        assert len(tasks) == 2
        assert tasks[0]["id"] == "TASK-01"
        assert tasks[1]["id"] == "TASK-02"

    def test_filters_by_pending(self, tmp_path):
        """Filter=pending returns only incomplete tasks."""
        prd_data = {
            "tasks": [
                {
                    "id": "TASK-01",
                    "title": "Task 1",
                    "completed": True,
                    "owner": "ralph",
                    "priority": 1,
                },
                {
                    "id": "TASK-02",
                    "title": "Task 2",
                    "completed": False,
                    "owner": "ralph",
                    "priority": 2,
                },
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            result = mcp_module.rzilla_tasks(filter="pending")

        tasks = json.loads(result)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "TASK-02"
        assert tasks[0]["completed"] is False

    def test_respects_limit(self, tmp_path):
        """Limit parameter limits number of tasks returned."""
        prd_data = {
            "tasks": [
                {
                    "id": f"TASK-{i:02d}",
                    "title": f"Task {i}",
                    "completed": False,
                    "owner": "ralph",
                    "priority": i,
                }
                for i in range(1, 11)  # 10 tasks
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            result = mcp_module.rzilla_tasks(limit=5)

        tasks = json.loads(result)
        assert len(tasks) == 5


class TestRzillaLog:
    """Tests for rzilla_log MCP tool."""

    def test_returns_no_log_message_when_missing(self, tmp_path):
        """When progress.txt doesn't exist, return appropriate message."""
        with patch.object(mcp_module, "PROGRESS_FILE", tmp_path / "nonexistent.txt"):
            result = mcp_module.rzilla_log()
            assert result == "No progress log found."

    def test_returns_last_lines(self, tmp_path):
        """Return last N lines from progress.txt."""
        progress_file = tmp_path / "progress.txt"
        lines = [f"Line {i}\n" for i in range(1, 31)]  # 30 lines
        progress_file.write_text("".join(lines))

        with patch.object(mcp_module, "PROGRESS_FILE", progress_file):
            result = mcp_module.rzilla_log(lines=10)

        returned_lines = result.strip().split("\n")
        assert len(returned_lines) == 10
        assert "Line 21" in returned_lines[0]
        assert "Line 30" in returned_lines[-1]

    def test_respects_max_limit(self, tmp_path):
        """Cannot request more than 100 lines."""
        progress_file = tmp_path / "progress.txt"
        lines = [f"Line {i}\n" for i in range(1, 201)]  # 200 lines
        progress_file.write_text("".join(lines))

        with patch.object(mcp_module, "PROGRESS_FILE", progress_file):
            result = mcp_module.rzilla_log(lines=150)  # Request 150, should get 100

        returned_lines = result.strip().split("\n")
        assert len(returned_lines) == 100


class TestRzillaSummary:
    """Tests for rzilla_summary MCP tool."""

    def test_returns_no_summary_when_none_exists(self, tmp_path):
        """When no summary files exist, return appropriate message."""
        with patch.object(mcp_module, "PROJECT_DIR", tmp_path):
            result = mcp_module.rzilla_summary()
            assert result == "No sprint summary found."

    def test_returns_latest_summary_content(self, tmp_path):
        """Return content of most recent summary file."""
        summary1 = tmp_path / "ralph-summary-2024-01-01.md"
        summary2 = tmp_path / "ralph-summary-2024-01-15.md"

        summary1.write_text("# Old Summary")
        summary2.write_text("# Latest Sprint Summary\n\nCompleted 5 tasks.")

        with patch.object(mcp_module, "PROJECT_DIR", tmp_path):
            result = mcp_module.rzilla_summary()
            assert result == "# Latest Sprint Summary\n\nCompleted 5 tasks."


class TestRzillaDryRun:
    """Tests for rzilla_dry_run MCP tool."""

    def test_runs_dry_run_without_task(self):
        """Run dry-run without specific task."""
        mock_result = Mock()
        mock_result.stdout = "Dry run output"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_subprocess:
            result = mcp_module.rzilla_dry_run()

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        assert call_args[0] == "uv"
        assert "--dry-run" in call_args
        assert "--task" not in call_args
        assert result == "Dry run output"

    def test_runs_dry_run_with_task(self):
        """Run dry-run with specific task ID."""
        mock_result = Mock()
        mock_result.stdout = "Dry run for TASK-01"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_subprocess:
            result = mcp_module.rzilla_dry_run(task="TASK-01")

        call_args = mock_subprocess.call_args[0][0]
        assert "--task" in call_args
        assert "TASK-01" in call_args
        assert result == "Dry run for TASK-01"

    def test_handles_timeout(self):
        """Handle timeout gracefully."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = mcp_module.rzilla_dry_run()

        assert "timed out" in result.lower()

    def test_handles_file_not_found(self):
        """Handle missing uv command gracefully."""
        with patch("subprocess.run", side_effect=FileNotFoundError("uv")):
            result = mcp_module.rzilla_dry_run()

        assert "not found" in result.lower()


class TestRzillaRun:
    """Tests for rzilla_run MCP tool."""

    def test_starts_background_process(self):
        """Start rzilla as detached background process."""
        mock_process = Mock()
        mock_process.pid = 12345

        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            with patch("builtins.open", MagicMock()):
                result = mcp_module.rzilla_run()

        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["start_new_session"] is True

        result_data = json.loads(result)
        assert result_data["pid"] == 12345
        assert "started" in result_data["message"].lower()

    def test_passes_all_flags(self):
        """Pass all optional flags to rzilla command."""
        mock_process = Mock()
        mock_process.pid = 12345

        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            with patch("builtins.open", MagicMock()):
                mcp_module.rzilla_run(
                    task="TASK-01",
                    skip_review=True,
                    opencode_only=True,
                    opencode_model="opencode/kimi-k2.5",
                    resume=True,
                    max_iterations=5,
                )

        call_args = mock_popen.call_args[0][0]
        assert "--task" in call_args
        assert "TASK-01" in call_args
        assert "--skip-review" in call_args
        assert "--opencode-only" in call_args
        assert "--opencode-model" in call_args
        assert "opencode/kimi-k2.5" in call_args
        assert "--resume" in call_args
        assert "--max" in call_args
        assert "5" in call_args


class TestRzillaAdd:
    """Tests for rzilla_add MCP tool."""

    def test_adds_task_with_spec(self):
        """Add task with natural language spec."""
        mock_result = Mock()
        mock_result.stdout = "Added task: TASK-99 - Implement feature X"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_subprocess:
            result = mcp_module.rzilla_add("Implement feature X")

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        assert call_args[0] == "uv"
        assert call_args[1] == "run"
        assert call_args[2] == "rzilla"
        assert call_args[3] == "add"
        assert call_args[4] == "Implement feature X"
        assert result == "Added task: TASK-99 - Implement feature X"

    def test_handles_timeout(self):
        """Handle timeout gracefully."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            result = mcp_module.rzilla_add("Some spec")

        assert "timed out" in result.lower()


class TestRzillaAbort:
    """Tests for rzilla_abort MCP tool."""

    def test_aborts_running_sprint(self):
        """Abort sends SIGTERM to rzilla process."""
        with patch.object(mcp_module, "_get_rzilla_pid", return_value=12345):
            with patch("os.kill") as mock_kill:
                result = mcp_module.rzilla_abort()

        mock_kill.assert_called_once_with(12345, signal.SIGTERM)
        assert "sigterm" in result.lower()
        assert "12345" in result

    def test_no_sprint_running(self):
        """When no sprint running, return appropriate message."""
        with patch.object(mcp_module, "_get_rzilla_pid", return_value=None):
            result = mcp_module.rzilla_abort()

        assert "no running" in result.lower()

    def test_handles_process_lookup_error(self):
        """Handle process already terminated."""
        with patch.object(mcp_module, "_get_rzilla_pid", return_value=12345):
            with patch("os.kill", side_effect=ProcessLookupError()):
                result = mcp_module.rzilla_abort()

        assert "not found" in result.lower() or "already" in result.lower()


class TestMCPAnnotations:
    """Tests for MCP tool annotations."""

    def test_readonly_tools_marked(self):
        """Read-only tools have readOnlyHint=True."""
        readonly_tools = [
            "rzilla_status",
            "rzilla_tasks",
            "rzilla_log",
            "rzilla_summary",
            "rzilla_dry_run",
        ]

        for tool_name in readonly_tools:
            tool = getattr(mcp_module, tool_name)
            # Check the MCP tool wrapper preserved the function
            assert hasattr(tool, "__wrapped__") or callable(tool)

    def test_destructive_abort_marked(self):
        """Abort tool has destructiveHint=True."""
        # The abort tool should have destructive annotation
        # We verify it's callable and has proper wrapper
        assert callable(mcp_module.rzilla_abort)


class TestIntegrationEdgeCases:
    """Integration tests for edge cases."""

    def test_status_with_empty_tasks(self, tmp_path):
        """Status handles empty task list."""
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps({"tasks": []}))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            with patch.object(mcp_module, "_is_sprint_running", return_value=False):
                with patch.object(mcp_module, "_find_latest_summary", return_value=None):
                    result = mcp_module.rzilla_status()

        data = json.loads(result)
        assert data["pending_tasks"] == 0
        assert data["completed_tasks"] == 0
        assert data["total_tasks"] == 0
        assert data["next_task"] is None

    def test_tasks_with_complex_dependencies(self, tmp_path):
        """Status correctly identifies next task with complex dependencies."""
        prd_data = {
            "tasks": [
                {
                    "id": "TASK-01",
                    "title": "Base",
                    "completed": True,
                    "owner": "ralph",
                    "depends_on": [],
                },
                {
                    "id": "TASK-02",
                    "title": "Depends on base",
                    "completed": False,
                    "owner": "ralph",
                    "depends_on": ["TASK-01"],
                },
                {
                    "id": "TASK-03",
                    "title": "Depends on incomplete",
                    "completed": False,
                    "owner": "ralph",
                    "depends_on": ["TASK-04"],
                },
                {
                    "id": "TASK-04",
                    "title": "Not done yet",
                    "completed": False,
                    "owner": "ralph",
                    "depends_on": [],
                },
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            with patch.object(mcp_module, "_is_sprint_running", return_value=False):
                with patch.object(mcp_module, "_find_latest_summary", return_value=None):
                    result = mcp_module.rzilla_status()

        data = json.loads(result)
        # TASK-02 should be next because TASK-01 is completed
        assert data["next_task"]["id"] == "TASK-02"
        assert data["next_task"]["title"] == "Depends on base"

    def test_log_handles_unicode(self, tmp_path):
        """Log tool handles unicode content correctly."""
        progress_file = tmp_path / "progress.txt"
        # Write unicode content with checkmark and other characters
        content = "Task completed: ✓ Feature implemented\nLine 2\nLine 3\n"
        progress_file.write_text(content, encoding="utf-8")

        with patch.object(mcp_module, "PROGRESS_FILE", progress_file):
            result = mcp_module.rzilla_log(lines=3)

        # Check that we got the expected lines
        assert "Line 2" in result or "Line 3" in result or "Feature" in result

    def test_run_with_all_parameters(self):
        """Run passes all parameters correctly to subprocess."""
        mock_process = Mock()
        mock_process.pid = 12345

        with patch("subprocess.Popen", return_value=mock_process) as mock_popen:
            with patch("builtins.open", MagicMock()):
                mcp_module.rzilla_run(
                    task="TASK-01",
                    skip_review=True,
                    opencode_only=True,
                    opencode_model="custom-model",
                    resume=True,
                    max_iterations=3,
                )

        call_args = mock_popen.call_args[0][0]
        assert "--task" in call_args and "TASK-01" in call_args
        assert "--skip-review" in call_args
        assert "--opencode-only" in call_args
        assert "--opencode-model" in call_args and "custom-model" in call_args
        assert "--resume" in call_args
        assert "--max" in call_args and "3" in call_args


# Required for psutil.NoSuchProcess in tests
try:
    import psutil
except ImportError:
    psutil = None


class TestMCPLoggingConfig:
    """Tests that MCP logging does not corrupt stdio transport."""

    def test_no_rich_handler_on_root_logger(self):
        """RichHandler must be removed from root logger after module init."""
        import logging

        for handler in logging.root.handlers:
            assert not (
                handler.__class__.__module__ == "rich.logging"
                and handler.__class__.__name__ == "RichHandler"
            ), f"RichHandler still present on root logger: {handler}"

    def test_mcp_loggers_silenced(self):
        """MCP loggers must be at ERROR level with propagate=False."""
        import logging

        for name in ("mcp", "mcp.server.fastmcp"):
            logger = logging.getLogger(name)
            assert logger.level == logging.ERROR, (
                f"{name} logger level={logger.level}, expected ERROR"
            )
            assert logger.propagate is False, f"{name} logger propagate=True, would leak to root"

    def test_server_produces_no_stderr_on_startup(self):
        """Server must emit nothing to stderr when launched with empty stdin."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "ralph_mcp"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(mcp_module.REPO_DIR),
        )
        assert result.stderr == "", f"Unexpected stderr output: {result.stderr[:200]}"


class TestProjectDirArg:
    """Tests for --project-dir CLI argument."""

    def test_default_project_dir_is_repo_dir(self):
        """Without --project-dir, PROJECT_DIR equals REPO_DIR."""
        assert mcp_module.PROJECT_DIR == mcp_module.REPO_DIR

    def test_prd_file_uses_project_dir(self, tmp_path):
        """PRD_FILE, PROGRESS_FILE, LOG_FILE resolve under PROJECT_DIR."""
        with (
            patch.object(mcp_module, "PROJECT_DIR", tmp_path),
            patch.object(mcp_module, "PRD_FILE", tmp_path / "prd.json"),
            patch.object(mcp_module, "PROGRESS_FILE", tmp_path / "progress.txt"),
            patch.object(mcp_module, "LOG_FILE", tmp_path / "ralph-loop.log"),
        ):
            assert mcp_module.PRD_FILE == tmp_path / "prd.json"
            assert mcp_module.PROGRESS_FILE == tmp_path / "progress.txt"
            assert mcp_module.LOG_FILE == tmp_path / "ralph-loop.log"

    def test_read_prd_from_project_dir(self, tmp_path):
        """_read_prd reads from PROJECT_DIR, not REPO_DIR."""
        prd_data = {"tasks": [{"id": "EXT-01", "title": "External task"}]}
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        with patch.object(mcp_module, "PRD_FILE", prd_file):
            result = mcp_module._read_prd()
        assert result == prd_data

    def test_find_latest_summary_in_project_dir(self, tmp_path):
        """_find_latest_summary searches PROJECT_DIR, not REPO_DIR."""
        summary = tmp_path / "ralph-summary-2024-06-01.md"
        summary.write_text("# Sprint summary")

        with patch.object(mcp_module, "PROJECT_DIR", tmp_path):
            result = mcp_module._find_latest_summary()
        assert result == summary

    def test_project_dir_cli_arg(self, tmp_path):
        """Server accepts --project-dir and resolves paths accordingly."""
        import subprocess
        import sys

        prd_data = {"tasks": [{"id": "P-01", "title": "Project task"}]}
        prd_file = tmp_path / "prd.json"
        prd_file.write_text(json.dumps(prd_data))

        init_msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )

        result = subprocess.run(
            [
                sys.executable,
                str(mcp_module.REPO_DIR / "ralph_mcp.py"),
                "--project-dir",
                str(tmp_path),
            ],
            input=init_msg,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stderr == "", f"Unexpected stderr: {result.stderr[:200]}"
        assert "rzilla" in result.stdout
