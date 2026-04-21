from unittest.mock import MagicMock

import pytest

from ralph import PlanChecker, PlanInvalidError


class TestPlanCheckerStructuralValidation:
    def test_structural_validation_fails_missing_required_field(self, tmp_path):
        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                }
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        task_tracker.load.return_value = prd

        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())
        errors = checker.check_structural(prd)

        missing_title = any("missing fields" in e and "title" in e for e in errors)
        assert missing_title

    def test_empty_acceptance_criteria_caught(self, tmp_path):
        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Test Task",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": [],
                    "owner": "ralph",
                    "completed": False,
                }
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        task_tracker.load.return_value = prd

        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())
        errors = checker.check_structural(prd)

        assert any("acceptance_criteria is empty" in e for e in errors)

    def test_unresolved_depends_on_raises_error(self, tmp_path):
        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Test Task",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                    "depends_on": ["NONEXISTENT"],
                }
            ]
        }
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        task_tracker.load.return_value = prd

        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())
        errors = checker.check_structural(prd)

        assert any("depends_on unknown task 'NONEXISTENT'" in e for e in errors)


class TestPlanCheckerComplexityInference:
    def test_complexity_inference_scores_1_for_simple_tasks(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())

        simple_task = {
            "description": "Simple task",
            "acceptance_criteria": ["AC1"],
            "files": [],
        }
        score = checker._infer_complexity(simple_task)
        assert score == 1

    def test_complexity_inference_scores_2_for_moderate_tasks(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())

        moderate_task = {
            "description": "Medium task description with more words",
            "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5"],
            "files": ["file1.py"],
        }
        score = checker._infer_complexity(moderate_task)
        assert score == 2

    def test_complexity_inference_scores_3_for_complex_tasks(self, tmp_path):
        prd_file = tmp_path / "prd.json"
        prd_file.write_text('{"tasks": []}')

        task_tracker = MagicMock()
        checker = PlanChecker(task_tracker, MagicMock(), MagicMock())

        complex_task = {
            "description": "This is a refactor task that requires redesign and migration "
            "of existing code to a new architecture with many components",
            "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"],
            "files": ["file1.py", "file2.py", "file3.py", "file4.py"],
        }
        score = checker._infer_complexity(complex_task)
        assert score == 3


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

    t1 = {
        "description": "Small task",
        "acceptance_criteria": ["AC1"],
    }
    assert checker._infer_complexity(t1) == 1

    t2 = {
        "description": "Medium task",
        "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5"],
    }
    assert checker._infer_complexity(t2) == 2

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
    prd = {"tasks": [{"id": "T1", "completed": False}]}
    checker = PlanChecker(MagicMock(), MagicMock(), MagicMock())

    with pytest.raises(PlanInvalidError):
        checker.run(prd)
