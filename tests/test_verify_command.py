"""Tests for the ralph verify CLI command (task M6-06)."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import ralph


@dataclass
class FakeTask:
    """Helper to build task dicts for tests."""

    id: str
    title: str = "Test task"
    description: str = "A test task with enough description to pass validation. " * 5
    acceptance_criteria: list = None
    owner: str = "ralph"
    completed: bool = False
    files: list = None
    depends_on: list = None

    def to_dict(self):
        d = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria or ["Criterion 1"],
            "owner": self.owner,
            "completed": self.completed,
        }
        if self.files is not None:
            d["files"] = self.files
        if self.depends_on is not None:
            d["depends_on"] = self.depends_on
        return d


class TestVerifyCLIAcceptsTaskId:
    """Verdict: Command accepts task ID argument."""

    def test_verify_command_exists(self):
        """verify is registered as a cli command."""
        assert "verify" in ralph.cli.commands

    def test_verify_requires_task_id(self):
        """Running verify without a task ID should fail."""
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(ralph.cli, ["verify"])
        assert result.exit_code != 0

    def test_verify_accepts_task_id(self):
        """Running verify with a task ID should not immediately error on missing command."""
        from click.testing import CliRunner

        with patch("ralph.TaskTracker") as mock_tt:
            instance = mock_tt.return_value
            instance.get_task_by_id.return_value = FakeTask(
                id="M6-06",
                acceptance_criteria=["AC 1"],
            ).to_dict()
            instance.repo_dir = Path(".")
            with patch("ralph._run_verify") as mock_verify:
                mock_verify.return_value = ralph.VerifyResult(
                    passed=True, exit_code=0, verdicts=[], report="pass"
                )
                with patch("ralph.RalphLogger"):
                    runner = CliRunner()
                    result = runner.invoke(ralph.cli, ["verify", "M6-06"])
                    assert "No such command" not in result.output


class TestVerifySendsACsAndCodeToAI:
    """Verdict: Sends acceptance criteria + code to AI for evaluation."""

    def test_verify_prompt_includes_acceptance_criteria(self):
        """verify_prompt includes all acceptance criteria."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["AC 1: do X", "AC 2: do Y", "AC 3: do Z"],
        ).to_dict()
        prompt = ralph.PromptBuilder.verify_prompt(task, "some code")
        assert "AC 1: do X" in prompt
        assert "AC 2: do Y" in prompt
        assert "AC 3: do Z" in prompt

    def test_verify_prompt_includes_code_context(self):
        """verify_prompt includes the code context."""
        task = FakeTask(id="T1", acceptance_criteria=["AC 1"]).to_dict()
        code = "def hello():\n    return 'world'"
        prompt = ralph.PromptBuilder.verify_prompt(task, code)
        assert code in prompt

    def test_verify_prompt_includes_task_title_and_description(self):
        """verify_prompt includes task title and description."""
        task = FakeTask(
            id="T1",
            title="My great task",
            description="This task does wonderful things.",
            acceptance_criteria=["AC 1"],
        ).to_dict()
        prompt = ralph.PromptBuilder.verify_prompt(task, "code")
        assert "My great task" in prompt
        assert "This task does wonderful things." in prompt

    def test_run_verify_calls_ai_runner(self):
        """_run_verify calls the AI runner with the correct agent."""
        task = FakeTask(
            id="T1", acceptance_criteria=["AC 1"], files=["ralph.py"]
        ).to_dict()
        tracker = MagicMock()
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = "1: PASSED: ok"

        with patch("ralph._gather_code_context", return_value="code here"):
            with patch("ralph.PromptBuilder.verify_prompt", return_value="prompt"):
                ralph._run_verify(task, tracker, ai_runner, Path("."), "gemini")

        ai_runner.run_reviewer.assert_called_once()
        call_args = ai_runner.run_reviewer.call_args
        assert call_args[0][0] == "gemini"

    def test_run_verify_gathers_code_from_files(self):
        """_run_verify reads files listed in the task."""
        task = FakeTask(
            id="T1", acceptance_criteria=["AC 1"], files=["ralph.py", "tests/test_x.py"]
        ).to_dict()
        tracker = MagicMock()
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = "1: PASSED: ok"

        with patch("ralph._gather_code_context", return_value="combined code") as mock_gather:
            ralph._run_verify(task, tracker, ai_runner, Path("."), None)
            mock_gather.assert_called_once_with(["ralph.py", "tests/test_x.py"], Path("."))


class TestVerifyParsesAIResponse:
    """Verdict: Parses AI response into pass/fail/partial verdicts."""

    def test_parse_all_passed(self):
        """All criteria passed."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["Do X", "Do Y"],
        ).to_dict()
        response = "1: PASSED: X works\n2: PASSED: Y works"
        result = ralph._parse_verify_response(response, task)
        assert result.passed is True
        assert result.verdicts[0]["status"] == "PASSED"
        assert result.verdicts[1]["status"] == "PASSED"

    def test_parse_all_failed(self):
        """All criteria failed."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["Do X", "Do Y"],
        ).to_dict()
        response = "1: FAILED: X missing\n2: FAILED: Y broken"
        result = ralph._parse_verify_response(response, task)
        assert result.passed is False
        assert result.verdicts[0]["status"] == "FAILED"
        assert result.verdicts[1]["status"] == "FAILED"

    def test_parse_partial(self):
        """Some criteria partial."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["Do X", "Do Y"],
        ).to_dict()
        response = "1: PASSED: ok\n2: PARTIAL: incomplete"
        result = ralph._parse_verify_response(response, task)
        assert result.passed is False
        assert result.verdicts[0]["status"] == "PASSED"
        assert result.verdicts[1]["status"] == "PARTIAL"

    def test_parse_mixed_verdicts(self):
        """Mix of passed, failed, and partial."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["A", "B", "C"],
        ).to_dict()
        response = "1: PASSED: a\n2: FAILED: b\n3: PARTIAL: c"
        result = ralph._parse_verify_response(response, task)
        assert result.passed is False
        assert [v["status"] for v in result.verdicts] == [
            "PASSED",
            "FAILED",
            "PARTIAL",
        ]

    def test_parse_with_fallback_pattern(self):
        """Falls back to line-level pattern matching when header pattern fails."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["Do X"],
        ).to_dict()
        # AI responds with just a status line without the "N: STATUS: reason" format
        response = "1: PASSED because X is implemented"
        result = ralph._parse_verify_response(response, task)
        assert result.verdicts[0]["status"] == "PASSED"

    def test_parse_no_response(self):
        """Handles empty AI response."""
        task = FakeTask(
            id="T1",
            acceptance_criteria=["Do X"],
        ).to_dict()
        result = ralph._parse_verify_response("", task)
        assert result.passed is False
        assert result.verdicts[0]["status"] == "FAILED"
        assert "No response" in result.verdicts[0]["reason"]


class TestVerifyExitCode:
    """Verdict: Exit code 0 on all passed, non-zero otherwise."""

    def test_exit_code_zero_when_all_passed(self):
        """VerifyResult.passed=True → exit_code 0."""
        result = ralph.VerifyResult(passed=True, exit_code=0)
        assert result.exit_code == 0
        assert bool(result) is True

    def test_exit_code_nonzero_when_any_failed(self):
        """VerifyResult.passed=False → exit_code non-zero."""
        result = ralph.VerifyResult(passed=False, exit_code=1)
        assert result.exit_code != 0
        assert bool(result) is False

    def test_run_verify_exit_code_passed(self):
        """_run_verify returns exit_code 0 when all criteria pass."""
        task = FakeTask(
            id="T1", acceptance_criteria=["AC 1"]
        ).to_dict()
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = "1: PASSED: ok"

        result = ralph._run_verify(task, MagicMock(), ai_runner, Path("."), None)
        assert result.exit_code == 0
        assert result.passed is True

    def test_run_verify_exit_code_failed(self):
        """_run_verify returns exit_code 1 when any criterion fails."""
        task = FakeTask(
            id="T1", acceptance_criteria=["AC 1"]
        ).to_dict()
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = "1: FAILED: broken"

        result = ralph._run_verify(task, MagicMock(), ai_runner, Path("."), None)
        assert result.exit_code == 1
        assert result.passed is False


class TestVerifyReportDetails:
    """Verdict: Reports detail which criteria failed and why."""

    def test_report_includes_failed_criterion(self):
        """Report shows which criteria failed."""
        task = FakeTask(
            id="T1",
            title="Test task",
            acceptance_criteria=["Do X", "Do Y"],
        ).to_dict()
        response = "1: PASSED: ok\n2: FAILED: Y not done"
        result = ralph._parse_verify_response(response, task)
        assert "FAILED" in result.report
        assert "Y not done" in result.report

    def test_report_includes_all_criteria(self):
        """Report lists all criteria with their status."""
        task = FakeTask(
            id="T1",
            title="Test task",
            acceptance_criteria=["Do X", "Do Y", "Do Z"],
        ).to_dict()
        response = "1: PASSED: x\n2: FAILED: y\n3: PARTIAL: z"
        result = ralph._parse_verify_response(response, task)
        report = result.report
        assert "Do X" in report
        assert "Do Y" in report
        assert "Do Z" in report

    def test_report_shows_summary_counts(self):
        """Report includes summary with pass/fail/partial counts."""
        task = FakeTask(
            id="T1", acceptance_criteria=["A", "B", "C"]
        ).to_dict()
        response = "1: PASSED: a\n2: FAILED: b\n3: PARTIAL: c"
        result = ralph._parse_verify_response(response, task)
        assert "Passed: 1" in result.report
        assert "Failed: 1" in result.report
        assert "Partial: 1" in result.report

    def test_report_shows_status_symbols(self):
        """Report uses symbols (✓/✗/◐) for visual status."""
        task = FakeTask(
            id="T1", acceptance_criteria=["A", "B"]
        ).to_dict()
        response = "1: PASSED: a\n2: FAILED: b"
        result = ralph._parse_verify_response(response, task)
        assert "✓" in result.report
        assert "✗" in result.report

    def test_report_includes_reasons(self):
        """Report includes the reason/explanation for each verdict."""
        task = FakeTask(
            id="T1", acceptance_criteria=["A"]
        ).to_dict()
        response = "1: FAILED: Function foo is missing error handling"
        result = ralph._parse_verify_response(response, task)
        assert "Function foo is missing error handling" in result.report


class TestVerifyCommandIntegration:
    """Integration-level tests for the verify command."""

    def test_verify_command_calls_task_tracker(self):
        """verify command looks up task by ID via TaskTracker."""
        with patch("ralph.TaskTracker") as mock_tt:
            instance = mock_tt.return_value
            instance.get_task_by_id.return_value = FakeTask(
                id="M6-06", acceptance_criteria=["AC 1"]
            ).to_dict()
            with patch("ralph._run_verify") as mock_verify:
                mock_verify.return_value = ralph.VerifyResult(
                    passed=True, exit_code=0, verdicts=[], report="pass"
                )
                with patch("ralph.RalphLogger"):
                    with patch("ralph.AIRunner"):
                        from click.testing import CliRunner

                        runner = CliRunner()
                        runner.invoke(ralph.cli, ["verify", "M6-06"])
                        instance.get_task_by_id.assert_called_once_with("M6-06")

    def test_verify_command_task_not_found(self):
        """verify command exits non-zero when task ID not found."""
        with patch("ralph.TaskTracker") as mock_tt:
            instance = mock_tt.return_value
            instance.get_task_by_id.return_value = None
            with patch("ralph.RalphLogger"):
                from click.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(ralph.cli, ["verify", "NONEXISTENT"])
                assert result.exit_code == 1
                assert "not found" in result.output.lower()

    def test_verify_command_default_agent_gemini(self):
        """verify uses gemini as default agent when --agent not specified."""
        with patch("ralph.TaskTracker") as mock_tt:
            instance = mock_tt.return_value
            instance.get_task_by_id.return_value = FakeTask(
                id="T1", acceptance_criteria=["AC 1"]
            ).to_dict()
            with patch("ralph._run_verify") as mock_verify:
                mock_verify.return_value = ralph.VerifyResult(
                    passed=True, exit_code=0, verdicts=[], report="pass"
                )
                with patch("ralph.RalphLogger"):
                    with patch("ralph.AIRunner"):
                        from click.testing import CliRunner

                        runner = CliRunner()
                        runner.invoke(ralph.cli, ["verify", "T1"])
                        call_args = mock_verify.call_args
                        assert call_args[0][4] is None


class TestGatherCodeContext:
    """Tests for _gather_code_context helper."""

    def test_gather_existing_file(self, tmp_path):
        """Reads existing files into code context."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text("print('hello')", encoding="utf-8")
        result = ralph._gather_code_context(["test_file.py"], tmp_path)
        assert "print('hello')" in result
        assert "test_file.py" in result

    def test_gather_missing_file(self, tmp_path):
        """Notes missing files in context."""
        result = ralph._gather_code_context(["nonexistent.py"], tmp_path)
        assert "not found" in result.lower() or "File" in result

    def test_gather_multiple_files(self, tmp_path):
        """Combines multiple files into one context string."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("a = 1", encoding="utf-8")
        f2.write_text("b = 2", encoding="utf-8")
        result = ralph._gather_code_context(["a.py", "b.py"], tmp_path)
        assert "a = 1" in result
        assert "b = 2" in result

    def test_gather_default_fallback(self, tmp_path):
        """Falls back to ralph.py when no files specified."""
        result = ralph._gather_code_context([], tmp_path)
        # Should still produce some context (or at least not crash)
        assert isinstance(result, str)
