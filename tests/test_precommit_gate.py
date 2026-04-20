from pathlib import Path
from unittest.mock import MagicMock

from ralph import PreCommitGate


def test_precommit_gate_success():
    runner = MagicMock()
    # Mock successful pre-commit run
    runner.run.return_value.returncode = 0

    ai_runner = MagicMock()
    config = MagicMock(max_precommit_rounds=2)
    gate = PreCommitGate(runner, ai_runner, MagicMock(), config)

    result = gate.run({}, {}, Path("."))
    assert result.passed is True
    assert result.rounds_used == 0


def test_precommit_gate_fail_then_success():
    runner = MagicMock()
    # 1. ruff fix, 2. ruff format, 3. pre-commit fail, 4. pre-commit success
    runner.run.side_effect = [
        MagicMock(returncode=0),  # ruff fix
        MagicMock(returncode=0),  # ruff format
        MagicMock(returncode=1, stdout="fail"),  # pre-commit fail
        MagicMock(returncode=0),  # pre-commit success
    ]

    ai_runner = MagicMock()
    ai_runner.assign_agents.return_value = ("coder", "reviewer")

    config = MagicMock(max_precommit_rounds=2)
    gate = PreCommitGate(runner, ai_runner, MagicMock(), config)

    result = gate.run({}, {}, Path("."))
    assert result.passed is True
    assert result.rounds_used == 1
    ai_runner.run_coder.assert_called_once()


def test_precommit_gate_max_rounds_reached():
    runner = MagicMock()
    runner.run.return_value.returncode = 1

    ai_runner = MagicMock()
    ai_runner.assign_agents.return_value = ("coder", "reviewer")

    config = MagicMock(max_precommit_rounds=2)
    gate = PreCommitGate(runner, ai_runner, MagicMock(), config)

    result = gate.run({}, {}, Path("."))
    assert result.passed is False
    assert result.rounds_used == 2
