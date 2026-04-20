import os
import subprocess

import pytest

from ralph import RalphLogger, SubprocessRunner


def test_subprocess_run_success(tmp_path, capsys):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    result = runner.run(["echo", "hello"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"

    captured = capsys.readouterr()
    assert "Running command: echo hello" in captured.out


def test_subprocess_run_check_failure(tmp_path):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    with pytest.raises(subprocess.CalledProcessError):
        runner.run(["ls", "/non-existent-directory"], check=True)


def test_subprocess_env_removals(tmp_path):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    os.environ["TEST_VAR"] = "present"

    # Verify it is present normally
    result = runner.run(["env"])
    assert "TEST_VAR=present" in result.stdout

    # Verify it is removed
    result = runner.run(["env"], env_removals=["TEST_VAR"])
    assert "TEST_VAR=present" not in result.stdout

    # Verify parent env is unchanged
    assert os.environ["TEST_VAR"] == "present"
    del os.environ["TEST_VAR"]


def test_subprocess_cwd(tmp_path):
    log_file = tmp_path / "ralph.log"
    logger = RalphLogger(log_file)
    runner = SubprocessRunner(logger)

    test_dir = tmp_path / "subdir"
    test_dir.mkdir()

    result = runner.run(["pwd"], cwd=test_dir)
    assert result.stdout.strip() == str(test_dir.resolve())
