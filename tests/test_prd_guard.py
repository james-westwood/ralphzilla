from unittest.mock import MagicMock

import pytest

from ralph import PRDGuard, PRDGuardViolation


def test_empty_prd_diff_passes():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = ""
    guard = PRDGuard(pr_manager, MagicMock())

    guard.check(42)

    pr_manager.get_diff_for_file.assert_called_once_with(42, "prd.json")


def test_any_prd_modification_raises_violation():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -10,1 +10,2 @@
+    "added_field": "value",
"""
    guard = PRDGuard(pr_manager, MagicMock())

    with pytest.raises(PRDGuardViolation) as excinfo:
        guard.check(42)

    assert "PR #42" in str(excinfo.value)
    assert "prd.json must not be modified" in str(excinfo.value)
    assert '"added_field"' in str(excinfo.value)


def test_completed_true_change_raises_violation():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -10,1 +10,2 @@
+    "completed": true,
"""
    guard = PRDGuard(pr_manager, MagicMock())

    with pytest.raises(PRDGuardViolation) as excinfo:
        guard.check(42)

    assert "PR #42" in str(excinfo.value)
    assert '"completed": true' in str(excinfo.value)


def test_violation_includes_pr_number_and_offending_lines():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -5,3 +5,5 @@
      "tasks": [
        {
+          "new_task": "added",
          "id": "M1-01",
-          "id": "M1-02",
+          "id": "M1-03",
"""
    guard = PRDGuard(pr_manager, MagicMock())

    with pytest.raises(PRDGuardViolation) as excinfo:
        guard.check(123)

    error_msg = str(excinfo.value)
    assert "PR #123" in error_msg
    assert "new_task" in error_msg or '"new_task"' in error_msg


def test_diff_with_only_deletions_passes():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -10,1 +10,0 @@
-    "removed_field": "value",
"""
    guard = PRDGuard(pr_manager, MagicMock())

    guard.check(42)

    pr_manager.get_diff_for_file.assert_called_once_with(42, "prd.json")
