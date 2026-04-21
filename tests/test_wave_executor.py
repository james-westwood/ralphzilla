"""Tests for WaveExecutor — wave-based parallel task runner."""

import asyncio

from ralph import ExecutionReport, TaskResult, WaveExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {"id": task_id, "depends_on": depends_on or []}


def ok_runner(task_id: str) -> TaskResult:
    return TaskResult(fatal=False, message=f"ok:{task_id}")


def fail_runner(task_id: str) -> TaskResult:
    return TaskResult(fatal=True, message=f"fail:{task_id}")


# ---------------------------------------------------------------------------
# build_waves — grouping
# ---------------------------------------------------------------------------


class TestBuildWaves:
    def test_independent_tasks_same_wave(self):
        tasks = [make_task("A"), make_task("B"), make_task("C")]
        executor = WaveExecutor(tasks)
        waves = executor.build_waves(["A", "B", "C"])
        assert len(waves) == 1
        assert set(waves[0]) == {"A", "B", "C"}

    def test_dependent_tasks_sequential_waves(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks)
        waves = executor.build_waves(["A", "B"])
        assert len(waves) == 2
        assert "A" in waves[0]
        assert "B" in waves[1]

    def test_linear_chain_three_waves(self):
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["B"])]
        executor = WaveExecutor(tasks)
        waves = executor.build_waves(["A", "B", "C"])
        assert len(waves) == 3
        assert waves[0] == ["A"]
        assert waves[1] == ["B"]
        assert waves[2] == ["C"]

    def test_diamond_two_waves(self):
        """A → B, A → C: A in wave 0, B+C in wave 1."""
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["A"])]
        executor = WaveExecutor(tasks)
        waves = executor.build_waves(["A", "B", "C"])
        assert len(waves) == 2
        assert waves[0] == ["A"]
        assert set(waves[1]) == {"B", "C"}

    def test_external_dependency_treated_as_satisfied(self):
        """Dep on a task outside task_ids — treated as already done."""
        tasks = [make_task("B", ["EXTERNAL"]), make_task("A")]
        executor = WaveExecutor(tasks)
        # EXTERNAL is not in the provided list, so B can be in wave 0
        waves = executor.build_waves(["A", "B"])
        assert len(waves) == 1
        assert set(waves[0]) == {"A", "B"}

    def test_empty_task_list(self):
        executor = WaveExecutor([])
        assert executor.build_waves([]) == []

    def test_single_task_one_wave(self):
        executor = WaveExecutor([make_task("X")])
        waves = executor.build_waves(["X"])
        assert waves == [["X"]]

    def test_wave_ordering_is_deterministic(self):
        """build_waves output is sorted — same input always same output."""
        tasks = [make_task("Z"), make_task("A"), make_task("M")]
        executor = WaveExecutor(tasks)
        waves1 = executor.build_waves(["Z", "A", "M"])
        waves2 = executor.build_waves(["M", "Z", "A"])
        assert waves1 == waves2
        assert waves1[0] == ["A", "M", "Z"]


# ---------------------------------------------------------------------------
# execute_wave — asyncio concurrency
# ---------------------------------------------------------------------------


class TestExecuteWave:
    def test_returns_result_for_each_task(self):
        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        results = executor.execute_wave(["A", "B"])
        assert set(results.keys()) == {"A", "B"}

    def test_successful_tasks_not_fatal(self):
        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        results = executor.execute_wave(["A", "B"])
        assert not results["A"].fatal
        assert not results["B"].fatal

    def test_failed_task_marked_fatal(self):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        results = executor.execute_wave(["A"])
        assert results["A"].fatal

    def test_runs_tasks_concurrently_using_asyncio(self):
        """Verify asyncio concurrency: tasks run in parallel, not sequentially."""
        import time

        started: list[float] = []

        async def slow_runner(task_id: str) -> TaskResult:
            started.append(time.monotonic())
            await asyncio.sleep(0.05)
            return TaskResult(fatal=False, message=task_id)

        tasks = [make_task("A"), make_task("B"), make_task("C")]
        executor = WaveExecutor(tasks, task_runner=slow_runner, max_workers=3)
        t0 = time.monotonic()
        results = executor.execute_wave(["A", "B", "C"])
        elapsed = time.monotonic() - t0
        # Parallel: all 3 tasks sleep 0.05s concurrently → total ≈ 0.05s
        # Sequential would take ≈ 0.15s.  Allow generous headroom.
        assert elapsed < 0.12, f"expected parallel execution, got {elapsed:.3f}s"
        assert set(results.keys()) == {"A", "B", "C"}

    def test_async_runner_is_supported(self):
        async def async_ok(task_id: str) -> TaskResult:
            await asyncio.sleep(0)
            return TaskResult(fatal=False, message=f"async:{task_id}")

        tasks = [make_task("X")]
        executor = WaveExecutor(tasks, task_runner=async_ok)
        results = executor.execute_wave(["X"])
        assert results["X"].message == "async:X"

    def test_empty_wave_returns_empty_dict(self):
        executor = WaveExecutor([])
        results = executor.execute_wave([])
        assert results == {}


# ---------------------------------------------------------------------------
# max_workers — limits concurrency
# ---------------------------------------------------------------------------


class TestMaxWorkers:
    def test_max_workers_limits_concurrency(self):
        """At most max_workers tasks run at the same time."""
        max_concurrent: list[int] = [0]
        active: list[int] = [0]

        async def counting_runner(task_id: str) -> TaskResult:
            active[0] += 1
            max_concurrent[0] = max(max_concurrent[0], active[0])
            await asyncio.sleep(0.02)
            active[0] -= 1
            return TaskResult(fatal=False, message=task_id)

        n_tasks = 6
        limit = 2
        tasks = [make_task(str(i)) for i in range(n_tasks)]
        executor = WaveExecutor(tasks, task_runner=counting_runner, max_workers=limit)
        executor.execute_wave([str(i) for i in range(n_tasks)])
        assert max_concurrent[0] <= limit

    def test_max_workers_one_serialises(self):
        """max_workers=1 → serial execution."""
        order: list[str] = []

        async def ordered_runner(task_id: str) -> TaskResult:
            order.append(f"start:{task_id}")
            await asyncio.sleep(0.01)
            order.append(f"end:{task_id}")
            return TaskResult(fatal=False, message=task_id)

        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ordered_runner, max_workers=1)
        executor.execute_wave(["A", "B"])
        # With max_workers=1 each task completes before the next starts
        starts = [e for e in order if e.startswith("start:")]
        ends = [e for e in order if e.startswith("end:")]
        assert len(starts) == 2
        assert len(ends) == 2
        # first task must end before second task starts
        assert order.index("end:" + starts[0][6:]) < order.index(starts[1])


# ---------------------------------------------------------------------------
# ExecutionReport
# ---------------------------------------------------------------------------


class TestExecutionReport:
    def test_report_contains_all_task_results(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert isinstance(report, ExecutionReport)
        assert set(report.results.keys()) == {"A", "B"}

    def test_report_waves_run_count(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert report.waves_run == 2

    def test_report_tasks_blocked_empty_on_success(self):
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert report.tasks_blocked == []

    def test_per_task_success_status(self):
        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        report = executor.run_parallel(tasks)
        assert not report.results["A"].fatal
        assert not report.results["B"].fatal

    def test_per_task_failure_status(self):
        tasks = [make_task("A")]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        report = executor.run_parallel(tasks)
        assert report.results["A"].fatal


# ---------------------------------------------------------------------------
# Failed tasks block dependents
# ---------------------------------------------------------------------------


class TestFailureBlocking:
    def test_failed_task_blocks_dependent_in_next_wave(self):
        """A fails → B (depends on A) is blocked, not executed."""
        tasks = [make_task("A"), make_task("B", ["A"])]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        report = executor.run_parallel(tasks)
        assert report.results["A"].fatal
        assert report.results["B"].fatal
        assert "B" in report.tasks_blocked

    def test_failed_task_does_not_block_independent_sibling(self):
        """A fails; C (independent) still runs successfully."""
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C")]

        def selective_runner(task_id: str) -> TaskResult:
            if task_id == "A":
                return TaskResult(fatal=True, message="fail")
            return TaskResult(fatal=False, message="ok")

        executor = WaveExecutor(tasks, task_runner=selective_runner)
        report = executor.run_parallel(tasks)
        assert report.results["A"].fatal
        assert "B" in report.tasks_blocked
        assert not report.results["C"].fatal

    def test_transitive_blocking(self):
        """A → B → C: A fails → B blocked → C blocked."""
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["B"])]
        executor = WaveExecutor(tasks, task_runner=fail_runner)
        report = executor.run_parallel(tasks)
        assert report.results["A"].fatal
        assert "B" in report.tasks_blocked
        assert "C" in report.tasks_blocked

    def test_wave_continues_despite_peer_failure(self):
        """In a wave with A and B, B's failure doesn't prevent A from completing."""
        tasks = [make_task("A"), make_task("B")]
        call_order: list[str] = []

        def tracking_runner(task_id: str) -> TaskResult:
            call_order.append(task_id)
            if task_id == "B":
                return TaskResult(fatal=True, message="fail")
            return TaskResult(fatal=False, message="ok")

        executor = WaveExecutor(tasks, task_runner=tracking_runner)
        report = executor.run_parallel(tasks)
        assert "A" in call_order
        assert "B" in call_order
        assert not report.results["A"].fatal
        assert report.results["B"].fatal
