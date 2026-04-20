import json
from unittest.mock import MagicMock, patch

from ralph import PRManager


def test_pr_create():
    runner = MagicMock()
    runner.run.return_value.stdout = "https://github.com/org/repo/pull/42\n"
    manager = PRManager(runner, MagicMock())

    pr = manager.create("branch", "title", "body")

    assert pr.number == 42
    assert pr.url == "https://github.com/org/repo/pull/42"
    runner.run.assert_called_with(
        ["gh", "pr", "create", "--branch", "branch", "--title", "title", "--body", "body"],
        check=True,
    )


def test_pr_get_existing():
    runner = MagicMock()
    runner.run.return_value.stdout = json.dumps([{"number": 42, "url": "https://github.com/42"}])
    manager = PRManager(runner, MagicMock())

    pr = manager.get_existing("branch")
    assert pr.number == 42
    assert pr.url == "https://github.com/42"


def test_pr_get_diff_retry():
    runner = MagicMock()
    # First two calls return empty diff, third call returns success
    runner.run.side_effect = [
        MagicMock(stdout=""),
        MagicMock(stdout=""),
        MagicMock(stdout="diff content"),
    ]
    manager = PRManager(runner, MagicMock())

    with patch("time.sleep") as mock_sleep:
        diff = manager.get_diff(42, retries=3, delay=1)
        assert diff == "diff content"
        assert mock_sleep.call_count == 2


def test_get_checks():
    runner = MagicMock()
    checks_data = [{"name": "CI", "state": "COMPLETED", "conclusion": "SUCCESS", "required": True}]
    runner.run.return_value.stdout = json.dumps(checks_data)
    manager = PRManager(runner, MagicMock())

    checks = manager.get_checks(42)
    assert checks == checks_data


def test_get_diff_for_file():
    runner = MagicMock()
    runner.run.return_value.stdout = """diff --git a/file1.py b/file1.py
index ...
--- a/file1.py
+++ b/file1.py
@@ -1 +1,2 @@
+new line
diff --git a/file2.py b/file2.py
index ...
--- a/file2.py
+++ b/file2.py
@@ -1 +1 @@
-old line
"""
    manager = PRManager(runner, MagicMock())

    diff1 = manager.get_diff_for_file(42, "file1.py")
    assert "file1.py" in diff1
    assert "file2.py" not in diff1

    diff2 = manager.get_diff_for_file(42, "file2.py")
    assert "file2.py" in diff2
    assert "file1.py" not in diff2
