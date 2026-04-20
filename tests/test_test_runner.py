from unittest.mock import MagicMock

from ralph import TestRunner


def test_test_runner_success():
    runner = MagicMock()
    runner.run.return_value.returncode = 0

    ai_runner = MagicMock()
    task_tracker = MagicMock()
    task_tracker.get_quality_checks.return_value = ["pytest tests/ -v"]

    config = MagicMock(max_test_fix_rounds=2)
    logger = MagicMock()

    test_runner = TestRunner(runner, ai_runner, task_tracker, logger, config)
    result = test_runner.run({}, {})

    assert result.passed is True
    assert result.rounds_used == 0
    task_tracker.get_quality_checks.assert_called_once()


def test_test_runner_all_checks_run():
    runner = MagicMock()
    runner.run.return_value.returncode = 0

    ai_runner = MagicMock()
    task_tracker = MagicMock()
    task_tracker.get_quality_checks.return_value = [
        "pytest tests/",
        "ruff check .",
    ]

    config = MagicMock(max_test_fix_rounds=2)
    logger = MagicMock()

    test_runner = TestRunner(runner, ai_runner, task_tracker, logger, config)
    result = test_runner.run({}, {})

    assert result.passed is True
    assert runner.run.call_count == 2


def test_test_runner_fail_then_success():
    runner = MagicMock()
    runner.run.side_effect = [
        MagicMock(returncode=1, stdout="test failed", stderr=""),
        MagicMock(returncode=0),
    ]

    ai_runner = MagicMock()
    ai_runner.assign_agents.return_value = ("coder", "reviewer")

    task_tracker = MagicMock()
    task_tracker.get_quality_checks.return_value = ["pytest tests/ -v"]

    config = MagicMock(max_test_fix_rounds=2)
    logger = MagicMock()

    test_runner = TestRunner(runner, ai_runner, task_tracker, logger, config)
    result = test_runner.run({}, {})

    assert result.passed is True
    assert result.rounds_used == 1
    ai_runner.run_coder.assert_called_once()


def test_test_runner_max_rounds_reached():
    runner = MagicMock()
    runner.run.return_value.returncode = 1

    ai_runner = MagicMock()
    ai_runner.assign_agents.return_value = ("coder", "reviewer")

    task_tracker = MagicMock()
    task_tracker.get_quality_checks.return_value = ["pytest tests/ -v"]

    config = MagicMock(max_test_fix_rounds=2)
    logger = MagicMock()

    test_runner = TestRunner(runner, ai_runner, task_tracker, logger, config)
    result = test_runner.run({}, {})

    assert result.passed is False
    assert result.rounds_used == 2
    assert ai_runner.run_coder.call_count == 1


def test_test_runner_fix_loop_fires_on_failure():
    runner = MagicMock()
    runner.run.return_value.returncode = 1

    ai_runner = MagicMock()
    ai_runner.assign_agents.return_value = ("claude", "reviewer")

    task_tracker = MagicMock()
    task_tracker.get_quality_checks.return_value = ["ruff check ."]

    config = MagicMock(max_test_fix_rounds=2)
    logger = MagicMock()

    test_runner = TestRunner(runner, ai_runner, task_tracker, logger, config)
    result = test_runner.run({"id": "TEST-01"}, {"quality_checks": ["ruff check ."]})

    assert result.passed is False
    ai_runner.run_coder.assert_called()
    call_args = ai_runner.run_coder.call_args
    assert call_args[0][0] == "claude"
