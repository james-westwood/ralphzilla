from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ralph import BranchExistsError, BranchManager, RemoteNotSSHError


def test_verify_ssh_remote_success():
    runner = MagicMock()
    runner.run.return_value.stdout = "git@github.com:org/repo.git\n"
    manager = BranchManager(Path("."), runner, MagicMock())

    manager.verify_ssh_remote()
    runner.run.assert_called_with(["git", "remote", "get-url", "origin"], cwd=Path("."))


def test_verify_ssh_remote_failure():
    runner = MagicMock()
    runner.run.return_value.stdout = "https://github.com/org/repo.git\n"
    manager = BranchManager(Path("."), runner, MagicMock())

    with pytest.raises(RemoteNotSSHError):
        manager.verify_ssh_remote()


def test_ensure_main_up_to_date():
    runner = MagicMock()
    manager = BranchManager(Path("."), runner, MagicMock())

    manager.ensure_main_up_to_date()

    assert runner.run.call_count == 3
    runner.run.assert_any_call(["git", "checkout", "main"], cwd=Path("."), check=True)
    runner.run.assert_any_call(["git", "fetch", "origin", "main"], cwd=Path("."), check=True)
    runner.run.assert_any_call(["git", "reset", "--hard", "origin/main"], cwd=Path("."), check=True)


def test_checkout_or_create_new():
    runner = MagicMock()
    # Mock branch list empty
    runner.run.side_effect = [
        MagicMock(stdout=""),  # git branch --list
        MagicMock(returncode=0),  # git checkout -b
        MagicMock(stdout="0\n"),  # git rev-list
    ]
    manager = BranchManager(Path("."), runner, MagicMock())

    status = manager.checkout_or_create("new-branch", resume=False)

    assert status.existed is False
    assert status.had_commits is False
    runner.run.assert_any_call(["git", "checkout", "-b", "new-branch"], cwd=Path("."), check=True)


def test_checkout_or_create_exists_no_resume():
    runner = MagicMock()
    runner.run.return_value.stdout = "  existing-branch\n"
    manager = BranchManager(Path("."), runner, MagicMock())

    with pytest.raises(BranchExistsError):
        manager.checkout_or_create("existing-branch", resume=False)


def test_checkout_or_create_exists_resume():
    runner = MagicMock()
    runner.run.side_effect = [
        MagicMock(stdout="  existing-branch\n"),  # git branch --list
        MagicMock(returncode=0),  # git checkout
        MagicMock(stdout="5\n"),  # git rev-list
    ]
    manager = BranchManager(Path("."), runner, MagicMock())

    status = manager.checkout_or_create("existing-branch", resume=True)

    assert status.existed is True
    assert status.had_commits is True
    runner.run.assert_any_call(["git", "checkout", "existing-branch"], cwd=Path("."), check=True)


def test_sanitise_branch_name():
    manager = BranchManager(Path("."), MagicMock(), MagicMock())
    assert manager.sanitise_branch_name("Feature: Add New UI!") == "feature--add-new-ui"
    assert len(manager.sanitise_branch_name("A" * 100)) == 40
