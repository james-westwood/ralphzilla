import pytest

from ralph import RalphLogger


def test_logger_creation(tmp_path):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    assert logger.log_path == log_file


def test_logger_info(tmp_path, capsys):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    logger.info("Test info message")

    # Check stdout
    captured = capsys.readouterr()
    assert "[INFO ] Test info message" in captured.out

    # Check log file
    content = log_file.read_text()
    assert "[INFO ] Test info message" in content
    # Check format YYYY-MM-DD HH:MM:SS
    assert len(content.split(" [INFO ]")[0]) == 19


def test_logger_multiple_levels(tmp_path, capsys):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    logger.warn("Test warn message")
    logger.error("Test error message")

    captured = capsys.readouterr()
    assert "[WARN ] Test warn message" in captured.out
    assert "[ERROR] Test error message" in captured.out

    content = log_file.read_text()
    assert "[WARN ] Test warn message" in content
    assert "[ERROR] Test error message" in content


def test_logger_fatal(tmp_path, capsys):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)

    with pytest.raises(SystemExit) as excinfo:
        logger.fatal("Test fatal message")

    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    assert "[FATAL] Test fatal message" in captured.out

    content = log_file.read_text()
    assert "[FATAL] Test fatal message" in content
