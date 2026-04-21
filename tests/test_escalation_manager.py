import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ralph import (
    BlockerKind,
    BlockerResult,
    EscalationManager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path, max_retries: int = 3, max_total: int = 5) -> EscalationManager:
    task_tracker = MagicMock()
    task_tracker.load = MagicMock(return_value={"tasks": [{"id": "M4-01", "title": "Existing"}]})
    task_tracker.add_task = MagicMock()
    logger = MagicMock()
    return EscalationManager(
        repo_dir=tmp_path,
        task_tracker=task_tracker,
        logger=logger,
        max_retries_per_blocker=max_retries,
        max_total_blockers=max_total,
    )


def _make_blocker(kind: BlockerKind = BlockerKind.CI_FATAL) -> BlockerResult:
    return BlockerResult(kind=kind, task_id="M4-01", context="some failure context")


def _make_task() -> dict:
    return {
        "id": "M4-01",
        "title": "Test Task",
        "description": "Does something important",
        "acceptance_criteria": ["Tests pass", "No regressions"],
        "epic": "M4",
    }


# ---------------------------------------------------------------------------
# test_tracks_consecutive_failures_per_blocker
# ---------------------------------------------------------------------------


class TestTracksConsecutiveFailuresPerBlocker:
    def test_starts_at_zero(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr._consecutive_failures == {}
        assert mgr._total_blockers == 0

    def test_record_failure_increments_count(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        assert mgr._consecutive_failures[BlockerKind.CI_FATAL.name] == 1
        assert mgr._total_blockers == 1

    def test_record_multiple_failures_same_kind(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        assert mgr._consecutive_failures[BlockerKind.CI_FATAL.name] == 3
        assert mgr._total_blockers == 3

    def test_records_different_blockers_independently(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.MERGE_CONFLICT)
        assert mgr._consecutive_failures[BlockerKind.CI_FATAL.name] == 2
        assert mgr._consecutive_failures[BlockerKind.MERGE_CONFLICT.name] == 1
        assert mgr._total_blockers == 3

    def test_reset_consecutive_clears_count(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.reset_consecutive(BlockerKind.CI_FATAL)
        assert mgr._consecutive_failures[BlockerKind.CI_FATAL.name] == 0
        # total is NOT reset by reset_consecutive
        assert mgr._total_blockers == 2


# ---------------------------------------------------------------------------
# test_escalates_after_three_retries_same_blocker
# ---------------------------------------------------------------------------


class TestEscalatesAfterThreeRetriesSameBlocker:
    def test_should_not_escalate_before_threshold(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=3)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        assert not mgr.should_escalate(BlockerKind.CI_FATAL)

    def test_should_escalate_at_threshold(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=3, max_total=99)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        assert mgr.should_escalate(BlockerKind.CI_FATAL)

    def test_should_escalate_above_threshold(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=3, max_total=99)
        for _ in range(5):
            mgr.record_failure(BlockerKind.CI_FATAL)
        assert mgr.should_escalate(BlockerKind.CI_FATAL)

    def test_escalation_is_per_kind(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=3, max_total=99)
        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)
        # Different kind has 0 consecutive — should not escalate on its own
        assert not mgr.should_escalate(BlockerKind.MERGE_CONFLICT)

    def test_reset_then_record_does_not_escalate(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=3, max_total=99)
        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.reset_consecutive(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        assert not mgr.should_escalate(BlockerKind.CI_FATAL)


# ---------------------------------------------------------------------------
# test_escalates_after_five_total_blockers
# ---------------------------------------------------------------------------


class TestEscalatesAfterFiveTotalBlockers:
    def test_should_not_escalate_before_total_threshold(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=99, max_total=5)
        for kind in [
            BlockerKind.CI_FATAL,
            BlockerKind.MERGE_CONFLICT,
            BlockerKind.PRD_GUARD_VIOLATION,
            BlockerKind.REVIEWER_UNAVAILABLE,
        ]:
            mgr.record_failure(kind)
        assert not mgr.should_escalate(BlockerKind.CI_FATAL)

    def test_should_escalate_at_total_threshold(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=99, max_total=5)
        kinds = [
            BlockerKind.CI_FATAL,
            BlockerKind.MERGE_CONFLICT,
            BlockerKind.PRD_GUARD_VIOLATION,
            BlockerKind.REVIEWER_UNAVAILABLE,
            BlockerKind.CI_FATAL,
        ]
        for kind in kinds:
            mgr.record_failure(kind)
        # Any blocker kind should trigger escalation now
        assert mgr.should_escalate(BlockerKind.CI_FATAL)
        assert mgr.should_escalate(BlockerKind.MERGE_CONFLICT)

    def test_total_threshold_respected_across_kinds(self, tmp_path):
        mgr = _make_manager(tmp_path, max_retries=99, max_total=3)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.MERGE_CONFLICT)
        assert not mgr.should_escalate(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.REVIEWER_UNAVAILABLE)
        assert mgr.should_escalate(BlockerKind.CI_FATAL)


# ---------------------------------------------------------------------------
# test_writes_escalation_markdown_with_context
# ---------------------------------------------------------------------------


class TestWritesEscalationMarkdownWithContext:
    def test_escalation_creates_markdown_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker()

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, "CI failed after 3 rounds")

        md_files = list(tmp_path.glob("escalation-*.md"))
        assert len(md_files) == 1

    def test_markdown_contains_task_id(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker()

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, "CI failed after 3 rounds")

        md_file = next(tmp_path.glob("escalation-*.md"))
        content = md_file.read_text(encoding="utf-8")
        assert "M4-01" in content

    def test_markdown_contains_blocker_kind(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, "CI failed")

        md_file = next(tmp_path.glob("escalation-*.md"))
        content = md_file.read_text(encoding="utf-8")
        assert "CI_FATAL" in content

    def test_markdown_contains_context(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker()
        context = "Unique context string for testing 12345"

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, context)

        md_file = next(tmp_path.glob("escalation-*.md"))
        content = md_file.read_text(encoding="utf-8")
        assert context in content

    def test_markdown_contains_acceptance_criteria(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker()

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, "failure")

        md_file = next(tmp_path.glob("escalation-*.md"))
        content = md_file.read_text(encoding="utf-8")
        assert "Tests pass" in content
        assert "No regressions" in content

    def test_markdown_filename_has_timestamp(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        task = _make_task()
        blocker = _make_blocker()

        with patch("ralph.click.echo"):
            mgr.escalate(task, blocker, "failure")

        md_files = list(tmp_path.glob("escalation-*.md"))
        assert len(md_files) == 1
        # filename format: escalation-20260421T123456.md
        name = md_files[0].name
        assert name.startswith("escalation-")
        assert name.endswith(".md")
        # timestamp portion: digits and T
        ts_part = name[len("escalation-") : -len(".md")]
        assert len(ts_part) == 15  # YYYYMMDDTHHmmss


# ---------------------------------------------------------------------------
# test_maintains_failure_ledger_json
# ---------------------------------------------------------------------------


class TestMaintainsFailureLedgerJson:
    def test_escalate_creates_ledger_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(), "failure ctx")

        ledger_path = tmp_path / ".ralph" / "escalations.json"
        assert ledger_path.exists()

    def test_ledger_contains_entry(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)
        mgr.record_failure(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(BlockerKind.CI_FATAL), "some context")

        ledger_path = tmp_path / ".ralph" / "escalations.json"
        entries = json.loads(ledger_path.read_text())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["task_id"] == "M4-01"
        assert entry["blocker_kind"] == "CI_FATAL"
        assert "some context" in entry["context"]

    def test_ledger_accumulates_multiple_escalations(self, tmp_path):
        mgr = _make_manager(tmp_path)

        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(BlockerKind.CI_FATAL), "first")

        # Reset and record more failures of a different kind
        mgr.reset_consecutive(BlockerKind.CI_FATAL)
        for _ in range(3):
            mgr.record_failure(BlockerKind.MERGE_CONFLICT)

        task2 = dict(_make_task(), id="M4-02", title="Second Task")
        blocker2 = BlockerResult(
            kind=BlockerKind.MERGE_CONFLICT, task_id="M4-02", context="conflict"
        )
        with patch("ralph.click.echo"):
            mgr.escalate(task2, blocker2, "second")

        ledger_path = tmp_path / ".ralph" / "escalations.json"
        entries = json.loads(ledger_path.read_text())
        assert len(entries) == 2
        assert entries[0]["blocker_kind"] == "CI_FATAL"
        assert entries[1]["blocker_kind"] == "MERGE_CONFLICT"

    def test_ledger_entry_has_required_fields(self, tmp_path):
        mgr = _make_manager(tmp_path)
        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(), "ctx")

        ledger_path = tmp_path / ".ralph" / "escalations.json"
        entry = json.loads(ledger_path.read_text())[0]
        required_fields = {
            "timestamp",
            "task_id",
            "task_title",
            "blocker_kind",
            "consecutive_failures",
            "total_sprint_blockers",
            "context",
        }
        assert required_fields.issubset(entry.keys())

    def test_ledger_survives_corrupt_json(self, tmp_path):
        """If .ralph/escalations.json is corrupt, escalate() overwrites it cleanly."""
        mgr = _make_manager(tmp_path)
        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)

        ledger_path = tmp_path / ".ralph" / "escalations.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text("not valid json", encoding="utf-8")

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(), "ctx")

        entries = json.loads(ledger_path.read_text())
        assert len(entries) == 1

    def test_escalate_creates_review_task(self, tmp_path):
        mgr = _make_manager(tmp_path)
        for _ in range(3):
            mgr.record_failure(BlockerKind.CI_FATAL)

        with patch("ralph.click.echo"):
            mgr.escalate(_make_task(), _make_blocker(), "ctx")

        mgr.task_tracker.add_task.assert_called_once()
        call_args = mgr.task_tracker.add_task.call_args[0][0]
        assert call_args["owner"] == "human"
        assert "REVIEW:" in call_args["title"]
        assert call_args["completed"] is False
