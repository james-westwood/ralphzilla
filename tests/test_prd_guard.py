from unittest.mock import MagicMock

import pytest

from ralph import PRDGuard, PRDGuardViolation


def test_prd_guard_clean():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = ""
    guard = PRDGuard(pr_manager, MagicMock())

    # Should not raise
    guard.check(42)


def test_prd_guard_violation():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -10,1 +10,2 @@
+    "completed": true,
"""
    guard = PRDGuard(pr_manager, MagicMock())

    with pytest.raises(PRDGuardViolation) as excinfo:
        guard.check(42)

    assert "PR #42 violated PRDGuard" in str(excinfo.value)
    assert '"completed": true,' in str(excinfo.value)


def test_prd_guard_any_addition_violation():
    pr_manager = MagicMock()
    pr_manager.get_diff_for_file.return_value = """--- a/prd.json
+++ b/prd.json
@@ -10,1 +10,2 @@
+    "some_other_field": "value",
"""
    guard = PRDGuard(pr_manager, MagicMock())

    with pytest.raises(PRDGuardViolation):
        guard.check(42)
