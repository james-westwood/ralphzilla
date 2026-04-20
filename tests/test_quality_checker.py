from pathlib import Path
from unittest.mock import MagicMock, patch

from ralph import TestQualityChecker


def test_constructor():
    """TestQualityChecker accepts ai_runner, logger, and config in constructor."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()

    tqc = TestQualityChecker(ai_runner, logger, config)

    assert tqc.ai_runner is ai_runner
    assert tqc.logger is logger
    assert tqc.config is config


def test_ast_checks_no_assertions():
    """_ast_checks detects test functions with no assertions."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    x = 1
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("no assertions" in i for i in issues)


def test_ast_checks_assert_true():
    """_ast_checks detects assert True patterns."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    assert True
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("trivially true" in i for i in issues)


def test_ast_checks_constant_assertion():
    """_ast_checks detects assert <constant> patterns."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    assert 42
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("constant assertion" in i for i in issues)


def test_ast_checks_pass_only_body():
    """_ast_checks detects pass-only test bodies."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    pass
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("empty or pass-only body" in i for i in issues)


def test_ast_checks_fewer_tests_than_acs():
    """_ast_checks detects test count < len(acceptance_criteria)."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_one():
    assert True
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1", "AC2", "AC3"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("Fewer tests" in i for i in issues)


def test_ast_checks_no_module_import():
    """_ast_checks detects no import of module under test."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    assert True
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert any("does not appear to import" in i for i in issues)


def test_ast_checks_valid_passes():
    """_ast_checks passes when test file is valid."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
import my_module

def test_something():
    result = my_module.func()
    assert result == expected
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}

    issues = tqc._ast_checks(test_source, task)

    assert len(issues) == 0


def test_check_returns_failed_result_when_ast_fails():
    """check() returns failed result immediately when AST checks fail."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
def test_something():
    pass
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")

    with patch("pathlib.Path.read_text", return_value=test_source):
        result = tqc.check(task, test_file_path)

    assert result.passed is False
    assert any("empty or pass-only body" in i for i in result.deterministic_issues)
    assert result.ai_issues == []
    ai_runner.run_reviewer.assert_not_called()


def test_check_calls_ai_when_ast_passes():
    """check() invokes AIRunner when AST checks pass."""
    ai_runner = MagicMock()
    ai_runner.run_reviewer.return_value = ""
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
import my_module

def test_something():
    assert my_module.func() == 1
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")

    with patch("pathlib.Path.read_text", return_value=test_source):
        tqc.check(task, test_file_path)

    ai_runner.run_reviewer.assert_called_once()


def test_check_parses_hollow_tests():
    """check() parses AI output for [HOLLOW] lines."""
    ai_runner = MagicMock()
    ai_runner.run_reviewer.return_value = "[HOLLOW] test_foo: checks implementation details"
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
import my_module

def test_foo():
    assert my_module._internal() == 1
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")

    with patch("pathlib.Path.read_text", return_value=test_source):
        result = tqc.check(task, test_file_path)

    assert result.passed is False
    assert "test_foo" in result.hollow_tests
    assert any("implementation details" in i for i in result.ai_issues)


def test_check_passes_when_no_hollow():
    """check() passes when no hollow tests found."""
    ai_runner = MagicMock()
    ai_runner.run_reviewer.return_value = "All tests look good."
    logger = MagicMock()
    config = MagicMock()
    tqc = TestQualityChecker(ai_runner, logger, config)

    test_source = """
import my_module

def test_something():
    assert my_module.func() == 1
"""
    task = {"title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")

    with patch("pathlib.Path.read_text", return_value=test_source):
        result = tqc.check(task, test_file_path)

    assert result.passed is True
    assert result.hollow_tests == []


def test_run_retries_test_writer():
    """run() retries TestWriter up to max_test_write_rounds."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    config.max_test_write_rounds = 2
    tqc = TestQualityChecker(ai_runner, logger, config)

    task = {"id": "TASK-01", "title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")
    test_writer = MagicMock()
    test_writer.write_tests.return_value = test_file_path

    with patch.object(tqc, "check") as mock_check:
        mock_check.side_effect = [
            MagicMock(passed=False),
            MagicMock(passed=True),
        ]

        tqc.run(task, test_file_path, test_writer, rounds=0)

        assert test_writer.write_tests.call_count == 1


def test_run_respects_max_rounds():
    """run() stops after max_test_write_rounds."""
    ai_runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    config.max_test_write_rounds = 2
    tqc = TestQualityChecker(ai_runner, logger, config)

    task = {"id": "TASK-01", "title": "my_module", "acceptance_criteria": ["AC1"]}
    test_file_path = Path("/fake/test.py")
    test_writer = MagicMock()

    mock_result = MagicMock(
        passed=False,
        hollow_tests=["test_foo"],
        deterministic_issues=[],
        ai_issues=[],
    )

    with patch.object(tqc, "check", return_value=mock_result):
        result = tqc.run(task, test_file_path, test_writer, rounds=0)

        assert result.passed is False
        assert result.rounds_used == 2
