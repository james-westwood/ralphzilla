"""Tests for WaveExecutor.print_wave_summary and ExecutionReport.wave_histories."""

from unittest.mock import patch

from ralph import TaskResult, WaveExecutor, WaveSummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {"id": task_id, "depends_on": depends_on or []}


def ok_runner(task_id: str) -> TaskResult:
    return TaskResult(fatal=False, message=f"ok:{task_id}")


def fail_runner(task_id: str) -> TaskResult:
    return TaskResult(fatal=True, message=f"fail:{task_id}")


def selective_fail_runner(failing: set[str]):
    def runner(task_id: str) -> TaskResult:
        if task_id in failing:
            return TaskResult(fatal=True, message=f"error in {task_id}")
        return TaskResult(fatal=False, message=f"ok:{task_id}")

    return runner


# ---------------------------------------------------------------------------
# print_wave_summary — output format
# ---------------------------------------------------------------------------


class TestPrintWaveSummary:
    """Unit tests for WaveExecutor.print_wave_summary()."""

    def _make_executor(self, task_ids: list[str]) -> WaveExecutor:
        tasks = [make_task(tid) for tid in task_ids]
        return WaveExecutor(tasks, task_runner=ok_runner)

    def test_summary_prints_after_each_wave(self, capsys):
        """print_wave_summary produces visible output."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=False, message="ok:A")}
        executor.print_wave_summary(results, wave_number=1)
        captured = capsys.readouterr()
        assert "Wave 1" in captured.out

    def test_summary_shows_wave_number(self, capsys):
        executor = self._make_executor(["X"])
        results = {"X": TaskResult(fatal=False)}
        executor.print_wave_summary(results, wave_number=3)
        assert "Wave 3" in capsys.readouterr().out

    def test_summary_shows_succeeded_count(self, capsys):
        executor = self._make_executor(["A", "B"])
        results = {
            "A": TaskResult(fatal=False),
            "B": TaskResult(fatal=False),
        }
        executor.print_wave_summary(results, wave_number=1)
        assert "2 succeeded" in capsys.readouterr().out

    def test_summary_shows_failed_count(self, capsys):
        executor = self._make_executor(["A", "B"])
        results = {
            "A": TaskResult(fatal=True, message="boom"),
            "B": TaskResult(fatal=False),
        }
        executor.print_wave_summary(results, wave_number=1)
        assert "1 failed" in capsys.readouterr().out

    def test_summary_shows_skipped_count(self, capsys):
        executor = self._make_executor(["A", "B"])
        results = {
            "A": TaskResult(fatal=False),
            "B": TaskResult(fatal=True, message="blocked: dependency failed"),
        }
        executor.print_wave_summary(results, wave_number=1, skipped_ids=["B"])
        out = capsys.readouterr().out
        assert "1 skipped" in out

    def test_per_task_breakdown_includes_task_id(self, capsys):
        executor = self._make_executor(["my-task"])
        results = {"my-task": TaskResult(fatal=False)}
        executor.print_wave_summary(results, wave_number=1)
        assert "my-task" in capsys.readouterr().out

    def test_per_task_success_shows_checkmark(self, capsys):
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=False)}
        executor.print_wave_summary(results, wave_number=1)
        assert "✓" in capsys.readouterr().out

    def test_per_task_failure_shows_cross(self, capsys):
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="something broke")}
        executor.print_wave_summary(results, wave_number=1)
        assert "✗" in capsys.readouterr().out

    def test_per_task_skipped_shows_circle(self, capsys):
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="blocked: dependency failed")}
        executor.print_wave_summary(results, wave_number=1, skipped_ids=["A"])
        assert "⊘" in capsys.readouterr().out

    def test_failed_task_shows_error_message(self, capsys):
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="assertion error on line 42")}
        executor.print_wave_summary(results, wave_number=1)
        assert "assertion error on line 42" in capsys.readouterr().out

    def test_skipped_task_does_not_show_error_message_as_failure(self, capsys):
        """Skipped (blocked) tasks appear under ⊘, not under ✗."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="blocked: dependency failed")}
        executor.print_wave_summary(results, wave_number=1, skipped_ids=["A"])
        out = capsys.readouterr().out
        assert "⊘" in out
        assert "✗" not in out

    def test_summary_uses_green_for_succeeded(self, capsys):
        """Color-coding: succeeded tasks are styled green."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=False)}
        with patch("click.style") as mock_style:
            mock_style.side_effect = lambda text, **kw: text
            executor.print_wave_summary(results, wave_number=1)
        green_calls = [c for c in mock_style.call_args_list if c.kwargs.get("fg") == "green"]
        assert green_calls, "expected at least one green-styled element"

    def test_summary_uses_red_for_failed(self, capsys):
        """Color-coding: failed tasks are styled red."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="boom")}
        with patch("click.style") as mock_style:
            mock_style.side_effect = lambda text, **kw: text
            executor.print_wave_summary(results, wave_number=1)
        red_calls = [c for c in mock_style.call_args_list if c.kwargs.get("fg") == "red"]
        assert red_calls, "expected at least one red-styled element"

    def test_summary_uses_yellow_for_skipped(self, capsys):
        """Color-coding: skipped tasks are styled yellow."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=True, message="blocked: dependency failed")}
        with patch("click.style") as mock_style:
            mock_style.side_effect = lambda text, **kw: text
            executor.print_wave_summary(results, wave_number=1, skipped_ids=["A"])
        yellow_calls = [c for c in mock_style.call_args_list if c.kwargs.get("fg") == "yellow"]
        assert yellow_calls, "expected at least one yellow-styled element"

    def test_no_skipped_ids_defaults_to_empty(self, capsys):
        """Calling without skipped_ids doesn't crash and counts 0 skipped."""
        executor = self._make_executor(["A"])
        results = {"A": TaskResult(fatal=False)}
        executor.print_wave_summary(results, wave_number=1)  # no skipped_ids
        assert "0 skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ExecutionReport.wave_histories — populated by run_parallel
# ---------------------------------------------------------------------------


class TestWaveHistories:
    def test_wave_histories_populated_after_run(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert len(report.wave_histories) == 2

    def test_wave_histories_count_matches_waves_run(self):
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["B"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert len(report.wave_histories) == report.waves_run

    def test_wave_histories_are_wave_summary_instances(self):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert all(isinstance(ws, WaveSummary) for ws in report.wave_histories)

    def test_wave_summary_wave_number_sequential(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        numbers = [ws.wave_number for ws in report.wave_histories]
        assert numbers == [1, 2]

    def test_wave_summary_succeeded_count_all_ok(self):
        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert report.wave_histories[0].succeeded == 2
        assert report.wave_histories[0].failed == 0
        assert report.wave_histories[0].skipped == 0

    def test_wave_summary_failed_count(self):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        report = executor.run_parallel(tasks)
        assert report.wave_histories[0].failed == 1
        assert report.wave_histories[0].succeeded == 0

    def test_wave_summary_skipped_count_blocked_deps(self):
        """A fails → B (depends on A) is skipped in wave 2."""
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        report = executor.run_parallel(tasks)
        wave2 = report.wave_histories[1]
        assert wave2.skipped == 1
        assert wave2.failed == 0

    def test_wave_summary_total_matches_tasks_in_wave(self):
        tasks = [make_task("A"), make_task("B"), make_task("C", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        wave1 = report.wave_histories[0]
        assert wave1.total == 2  # A and B in first wave

    def test_wave_summary_results_contains_task_results(self):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert "A" in report.wave_histories[0].results
        assert isinstance(report.wave_histories[0].results["A"], TaskResult)

    def test_wave_histories_empty_list_on_no_tasks(self):
        executor = WaveExecutor([])
        report = executor.run_parallel([])
        assert report.wave_histories == []

    def test_wave_summary_captures_error_message(self):
        """Failed task's error message is accessible in wave_histories."""
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=selective_fail_runner({"A"}))
        report = executor.run_parallel(tasks)
        result = report.wave_histories[0].results["A"]
        assert result.fatal
        assert "error in A" in result.message


# ---------------------------------------------------------------------------
# Integration: summary prints after each wave during run_parallel
# ---------------------------------------------------------------------------


class TestWaveSummaryIntegration:
    def test_summary_printed_for_each_wave(self, capsys):
        """run_parallel prints a summary after every wave."""
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["B"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        executor.run_parallel(tasks)
        out = capsys.readouterr().out
        assert "Wave 1" in out
        assert "Wave 2" in out
        assert "Wave 3" in out

    def test_summary_printed_before_next_wave_starts(self, capsys):
        """Wave N summary appears before Wave N+1 output."""
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        executor.run_parallel(tasks)
        out = capsys.readouterr().out
        assert out.index("Wave 1") < out.index("Wave 2")

    def test_failed_task_error_visible_in_run_parallel_output(self, capsys):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=selective_fail_runner({"A"}))
        executor.run_parallel(tasks)
        assert "error in A" in capsys.readouterr().out
