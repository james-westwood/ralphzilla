from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ralph import BranchExistsError, BranchManager, RemoteNotSSHError


def test_verify_ssh_remote_raises_on_https_url():
    runner = MagicMock()
    runner.run.return_value.stdout = "https://github.com/org/repo.git\n"
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    with pytest.raises(RemoteNotSSHError) as excinfo:
        manager.verify_ssh_remote()

    assert "HTTPS remote detected" in str(excinfo.value)
    runner.run.assert_called_once_with(["git", "remote", "get-url", "origin"], cwd=Path("."))


def test_verify_ssh_remote_success():
    runner = MagicMock()
    runner.run.return_value.stdout = "git@github.com:org/repo.git\n"
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    manager.verify_ssh_remote()
    runner.run.assert_called_once_with(["git", "remote", "get-url", "origin"], cwd=Path("."))


def test_ensure_main_up_to_date_uses_reset_not_pull():
    runner = MagicMock()
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    manager.ensure_main_up_to_date()

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]

    assert ["git", "checkout", "main"] in cmd_lists
    assert ["git", "fetch", "origin", "main"] in cmd_lists
    assert ["git", "reset", "--hard", "origin/main"] in cmd_lists

    for cmd in cmd_lists:
        assert "pull" not in cmd, "ensure_main_up_to_date must use reset --hard, not pull"
        assert "--ff-only" not in cmd, (
            "ensure_main_up_to_date must use reset --hard, not pull --ff-only"
        )


def test_checkout_or_create_raises_on_existing_branch_no_resume():
    runner = MagicMock()
    runner.run.return_value.stdout = "  existing-branch\n"
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    with pytest.raises(BranchExistsError) as excinfo:
        manager.checkout_or_create("existing-branch", resume=False)

    assert "existing-branch" in str(excinfo.value)
    assert "resume=False" in str(excinfo.value)

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]
    assert ["git", "branch", "--list", "existing-branch"] in cmd_lists


def test_checkout_or_create_creates_new_branch_when_not_exists():
    runner = MagicMock()
    runner.run.side_effect = [
        MagicMock(stdout=""),  # git branch --list (empty - branch doesn't exist)
        MagicMock(returncode=0),  # git checkout -b
        MagicMock(stdout="0\n"),  # git rev-list (no commits)
    ]
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    status = manager.checkout_or_create("new-branch", resume=False)

    assert status.existed is False
    assert status.had_commits is False

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]
    assert ["git", "checkout", "-b", "new-branch"] in cmd_lists


def test_checkout_or_create_resumes_existing_branch():
    runner = MagicMock()
    runner.run.side_effect = [
        MagicMock(stdout="  existing-branch\n"),  # git branch --list
        MagicMock(returncode=0),  # git checkout
        MagicMock(stdout="5\n"),  # git rev-list (has commits)
    ]
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    status = manager.checkout_or_create("existing-branch", resume=True)

    assert status.existed is True
    assert status.had_commits is True

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]
    assert ["git", "checkout", "existing-branch"] in cmd_lists


def test_sanitise_branch_name_truncates_to_40_chars():
    logger = MagicMock()
    manager = BranchManager(Path("."), MagicMock(), logger)

    result = manager.sanitise_branch_name("A" * 100)
    assert len(result) == 40


def test_sanitise_branch_name_transforms_title():
    logger = MagicMock()
    manager = BranchManager(Path("."), MagicMock(), logger)

    result = manager.sanitise_branch_name("Feature: Add New UI!")
    assert result == "feature--add-new-ui"
    assert len(result) <= 40


def test_push_verifies_ssh_before_executing():
    runner = MagicMock()
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    runner.run.return_value.stdout = "git@github.com:org/repo.git\n"

    manager.push_branch("test-branch")

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]

    verify_ssh_call = [c for c in cmd_lists if "remote" in c and "get-url" in c]
    push_call = [c for c in cmd_lists if c[0] == "git" and c[1] == "push"]

    assert len(verify_ssh_call) == 1, "verify_ssh_remote must be called before push"
    assert len(push_call) == 1, "push command must be executed"

    verify_ssh_index = cmd_lists.index(verify_ssh_call[0])
    push_index = cmd_lists.index(push_call[0])

    assert verify_ssh_index < push_index, "verify_ssh_remote must be called before push"


def test_delete_remote_verifies_ssh():
    runner = MagicMock()
    logger = MagicMock()
    manager = BranchManager(Path("."), runner, logger)

    runner.run.return_value.stdout = "git@github.com:org/repo.git\n"

    manager.delete_remote("test-branch")

    calls = runner.run.call_args_list
    cmd_lists = [c[0][0] for c in calls]

    verify_ssh_call = [c for c in cmd_lists if "remote" in c and "get-url" in c]

    assert len(verify_ssh_call) == 1, "verify_ssh_remote must be called before delete"
    assert verify_ssh_call[0] == ["git", "remote", "get-url", "origin"]
