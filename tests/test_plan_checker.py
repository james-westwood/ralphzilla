from unittest.mock import MagicMock

import pytest

from ralph import PlanChecker, PlanInvalidError


def test_check_structural_valid():
    prd = {
        "tasks": [
            {
                "id": "T1",
                "title": "T1",
                "description": "This is a valid task description that is definitely longer "
                "than one hundred characters to satisfy the prd validator rule.",
                "acceptance_criteria": ["Must update tests/test_module.py"],
                "owner": "ralph",
                "completed": False,
            }
        ]
    }
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())
    errors = checker.check_structural(prd)
    assert not errors


def test_check_structural_missing_field():
    prd = {
        "tasks": [
            {
                "id": "T1",
                "title": "T1",
                # missing description
                "acceptance_criteria": ["AC1"],
                "owner": "ralph",
                "completed": False,
            }
        ]
    }
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())
    errors = checker.check_structural(prd)
    assert any("missing fields" in e and "description" in e for e in errors)


def test_check_structural_empty_ac():
    prd = {
        "tasks": [
            {
                "id": "T1",
                "title": "T1",
                "description": "This is a valid task description that is definitely longer "
                "than one hundred characters to satisfy the prd validator rule.",
                "acceptance_criteria": [],
                "owner": "ralph",
                "completed": False,
            }
        ]
    }
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())
    errors = checker.check_structural(prd)
    assert any("acceptance_criteria is empty" in e for e in errors)


def test_check_structural_unresolved_dep():
    prd = {
        "tasks": [
            {
                "id": "T1",
                "title": "T1",
                "description": "This is a valid task description that is definitely longer "
                "than one hundred characters to satisfy the prd validator rule.",
                "acceptance_criteria": ["Must update tests/test_module.py"],
                "owner": "ralph",
                "completed": False,
                "depends_on": ["T2"],
            }
        ]
    }
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())
    errors = checker.check_structural(prd)
    assert any("depends_on unknown task 'T2'" in e for e in errors)


def test_infer_complexity():
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())

    # Complexity 1
    t1 = {
        "description": "Small task",
        "acceptance_criteria": ["AC1"],
    }
    assert checker._infer_complexity(t1) == 1

    # Complexity 2 (AC count > 4)
    t2 = {
        "description": "Medium task",
        "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5"],
    }
    assert checker._infer_complexity(t2) == 2

    # Complexity 3 (Many reasons)
    t3 = {
        "description": "Large refactor task " + "word " * 100,
        "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5"],
        "files": ["f1.py", "f2.py", "f3.py", "f4.py"],
    }
    assert checker._infer_complexity(t3) == 3


def test_auto_decompose():
    task_tracker = MagicMock()
    ai_runner = MagicMock()
    ai_runner.run_decompose.return_value = [
        {
            "title": "Subtask 1",
            "description": "Desc 1",
            "acceptance_criteria": ["AC1"],
            "owner": "ralph",
        },
        {
            "title": "Subtask 2",
            "description": "Desc 2",
            "acceptance_criteria": ["AC2"],
            "owner": "ralph",
        },
    ]

    prd = {
        "tasks": [
            {
                "id": "T1",
                "title": "Big Task",
                "description": "Big desc " + "word " * 100,
                "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5"],
                "owner": "ralph",
                "completed": False,
                "complexity": 3,
            }
        ]
    }

    checker = PlanChecker(task_tracker, ai_runner, MagicMock())
    count = checker.auto_decompose(prd)

    assert count == 1
    assert task_tracker.add_task.call_count == 2
    assert task_tracker.mark_decomposed.called


def test_run_raises_invalid_plan():
    prd = {"tasks": [{"id": "T1", "completed": False}]}  # missing fields
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())

    with pytest.raises(PlanInvalidError):
        checker.run(prd)
