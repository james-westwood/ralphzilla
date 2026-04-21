"""Tests for ConflictDetector and its integration with WaveExecutor."""

import pytest

from ralph import ConflictDetector, ConflictReport, TaskResult, WaveConflictError, WaveExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(
    task_id: str, files: list[str] | None = None, depends_on: list[str] | None = None
) -> dict:
    t: dict = {"id": task_id, "depends_on": depends_on or []}
    if files is not None:
        t["files"] = files
    return t


def ok_runner(task_id: str) -> TaskResult:
    return TaskResult(fatal=False, message=f"ok:{task_id}")


# ---------------------------------------------------------------------------
# ConflictDetector.check_wave_conflicts — basic detection
# ---------------------------------------------------------------------------


class TestCheckWaveConflicts:
    def test_no_conflicts_disjoint_files(self):
        tasks = [
            make_task("A", files=["src/foo.py"]),
            make_task("B", files=["src/bar.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert isinstance(report, ConflictReport)
        assert not report.has_conflicts
        assert report.conflicting_tasks == []
        assert report.shared_files == {}

    def test_no_conflicts_empty_files(self):
        tasks = [make_task("A"), make_task("B")]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert not report.has_conflicts

    def test_no_conflicts_single_task(self):
        tasks = [make_task("A", files=["src/foo.py"])]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert not report.has_conflicts

    def test_detects_shared_file(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert report.has_conflicts

    def test_shared_files_maps_file_to_task_ids(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert "src/shared.py" in report.shared_files
        assert set(report.shared_files["src/shared.py"]) == {"A", "B"}

    def test_conflicting_tasks_contains_correct_pair(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert len(report.conflicting_tasks) == 1
        pair = report.conflicting_tasks[0]
        assert set(pair) == {"A", "B"}

    def test_multiple_shared_files(self):
        tasks = [
            make_task("A", files=["src/foo.py", "src/bar.py"]),
            make_task("B", files=["src/foo.py"]),
            make_task("C", files=["src/bar.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert report.has_conflicts
        assert "src/foo.py" in report.shared_files
        assert "src/bar.py" in report.shared_files

    def test_three_tasks_sharing_one_file(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
            make_task("C", files=["src/shared.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert report.has_conflicts
        # Three unique pairs: (A,B), (A,C), (B,C)
        assert len(report.conflicting_tasks) == 3
        pairs_as_sets = [frozenset(p) for p in report.conflicting_tasks]
        assert frozenset({"A", "B"}) in pairs_as_sets
        assert frozenset({"A", "C"}) in pairs_as_sets
        assert frozenset({"B", "C"}) in pairs_as_sets

    def test_no_duplicate_pairs(self):
        """A file shared by A and B should produce exactly one pair, not two."""
        tasks = [
            make_task("A", files=["f.py", "g.py"]),
            make_task("B", files=["f.py", "g.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        # Even though two files are shared, the pair (A, B) should appear once
        pairs_as_sets = [frozenset(p) for p in report.conflicting_tasks]
        assert pairs_as_sets.count(frozenset({"A", "B"})) == 1

    def test_empty_task_list(self):
        detector = ConflictDetector()
        report = detector.check_wave_conflicts([])
        assert not report.has_conflicts
        assert report.conflicting_tasks == []
        assert report.shared_files == {}

    def test_non_overlapping_mix(self):
        tasks = [
            make_task("A", files=["src/a.py"]),
            make_task("B", files=["src/b.py"]),
            make_task("C", files=["src/c.py", "src/a.py"]),
        ]
        detector = ConflictDetector()
        report = detector.check_wave_conflicts(tasks)
        assert report.has_conflicts
        assert set(report.shared_files["src/a.py"]) == {"A", "C"}
        # B is not involved in any conflict
        all_task_ids = {tid for pair in report.conflicting_tasks for tid in pair}
        assert "B" not in all_task_ids


# ---------------------------------------------------------------------------
# WaveExecutor.build_waves — conflict-aware splitting
# ---------------------------------------------------------------------------


class TestBuildWavesConflictSplitting:
    def test_conflicting_tasks_placed_in_separate_waves(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        waves = executor.build_waves(["A", "B"])
        # A and B must NOT be in the same wave
        for wave in waves:
            assert not ({"A", "B"} <= set(wave)), "A and B share a file — must be in separate waves"
        # Both tasks appear exactly once across all waves
        all_ids = [tid for wave in waves for tid in wave]
        assert sorted(all_ids) == ["A", "B"]

    def test_non_conflicting_tasks_stay_in_same_wave(self):
        tasks = [
            make_task("A", files=["src/a.py"]),
            make_task("B", files=["src/b.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        waves = executor.build_waves(["A", "B"])
        assert len(waves) == 1
        assert set(waves[0]) == {"A", "B"}

    def test_three_tasks_all_conflicting_three_waves(self):
        """A, B, C all share the same file — each in its own wave."""
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
            make_task("C", files=["src/shared.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        waves = executor.build_waves(["A", "B", "C"])
        for wave in waves:
            assert len(wave) == 1, "all three conflict — each must be alone in its wave"

    def test_partial_conflict_splits_only_conflicting_pair(self):
        """A and B conflict; C is independent — C can share a wave with A or B."""
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
            make_task("C", files=["src/c.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        waves = executor.build_waves(["A", "B", "C"])
        for wave in waves:
            assert not ({"A", "B"} <= set(wave)), "A and B must not be in the same wave"
        all_ids = sorted(tid for wave in waves for tid in wave)
        assert all_ids == ["A", "B", "C"]

    def test_dependency_then_conflict(self):
        """C depends on A; A and B conflict. A→wave0 (split from B), B→wave0 or 1, C→after A."""
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
            make_task("C", depends_on=["A"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        waves = executor.build_waves(["A", "B", "C"])
        # A and B must never be in the same wave
        for wave in waves:
            assert not ({"A", "B"} <= set(wave))
        # C must appear after A
        wave_for = {tid: i for i, wave in enumerate(waves) for tid in wave}
        assert wave_for["C"] > wave_for["A"]
        all_ids = sorted(tid for wave in waves for tid in wave)
        assert all_ids == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# WaveExecutor.execute_wave — raises WaveConflictError
# ---------------------------------------------------------------------------


class TestExecuteWaveConflictGuard:
    def test_raises_on_conflicting_wave(self):
        tasks = [
            make_task("A", files=["src/shared.py"]),
            make_task("B", files=["src/shared.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        with pytest.raises(WaveConflictError):
            executor.execute_wave(["A", "B"])

    def test_no_error_for_disjoint_files(self):
        tasks = [
            make_task("A", files=["src/a.py"]),
            make_task("B", files=["src/b.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        results = executor.execute_wave(["A", "B"])
        assert set(results.keys()) == {"A", "B"}

    def test_no_error_for_tasks_without_files(self):
        tasks = [make_task("A"), make_task("B")]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        results = executor.execute_wave(["A", "B"])
        assert set(results.keys()) == {"A", "B"}

    def test_error_message_contains_conflicting_pair(self):
        tasks = [
            make_task("X", files=["common.py"]),
            make_task("Y", files=["common.py"]),
        ]
        executor = WaveExecutor(tasks, task_runner=ok_runner)
        with pytest.raises(WaveConflictError, match="X"):
            executor.execute_wave(["X", "Y"])
