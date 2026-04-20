from ralph import PromptBuilder


def test_coder_prompt():
    task = {
        "title": "Task 1",
        "description": "Desc 1",
        "acceptance_criteria": ["AC1", "AC2"],
        "files": ["file1.py"],
        "epic": "E1",
    }
    prd = {"epic_addenda": {"E1": "Addendum 1"}}

    prompt = PromptBuilder.coder_prompt(task, "Claude", prd)
    assert "Task 1" in prompt
    assert "AC1" in prompt
    assert "1." in prompt
    assert "2." in prompt
    assert "file1.py" in prompt
    assert "Addendum 1" in prompt
    assert "Do NOT touch prd.json" in prompt


def test_coder_prompt_resume():
    task = {"title": "T1"}
    prompt = PromptBuilder.coder_prompt(task, "C", {}, resume=True)
    assert "already has commits" in prompt


def test_reviewer_prompt_with_addenda():
    task = {"title": "T1", "description": "D1", "epic": "GUI"}
    prd = {"epic_addenda": {"GUI": "Check for GTK4 runtime errors."}}
    prompt = PromptBuilder.reviewer_prompt(task, "diff content", prd, 1)
    assert "Correctness" in prompt
    assert "Security" in prompt
    assert "Performance" in prompt
    assert "Maintainability" in prompt
    assert "Testing" in prompt
    assert "PRD adherence" in prompt
    assert "GTK4 runtime" in prompt
    assert "APPROVED" in prompt
    assert "CHANGES REQUESTED" in prompt
    assert "file+line" in prompt


def test_reviewer_prompt_without_addenda():
    task = {"title": "T1", "description": "D1"}
    prompt = PromptBuilder.reviewer_prompt(task, "diff", {}, 1)
    assert "Correctness" in prompt


def test_test_writer_prompt():
    task = {"title": "T1", "description": "D1", "acceptance_criteria": ["AC1"]}
    prompt = PromptBuilder.test_writer_prompt(task)
    assert "Write failing tests only" in prompt
    assert "Do NOT implement the module" in prompt
    assert "ImportError or AssertionError" in prompt


def test_test_quality_prompt():
    task = {"title": "T1", "acceptance_criteria": ["AC1"]}
    prompt = PromptBuilder.test_quality_prompt(task, "test source", "ast report")
    assert "HOLLOW" in prompt
    assert "AC1" in prompt


def test_decompose_prompt():
    task = {"title": "Big Task"}
    prompt = PromptBuilder.decompose_prompt(task)
    assert "Break the following complexity-3 task" in prompt
    assert "JSON list" in prompt


def test_plan_check_prompt():
    tasks = [{"id": "T1", "title": "Task 1"}]
    prompt = PromptBuilder.plan_check_prompt(tasks)
    assert "[WARN]" in prompt
    assert "not atomic" in prompt
    assert "untestable" in prompt


def test_precommit_fix_prompt():
    task = {"title": "Task 1"}
    prompt = PromptBuilder.precommit_fix_prompt(task, "error output")
    assert "Task 1" in prompt
    assert "pre-commit" in prompt
    assert "error output" in prompt


def test_test_fix_prompt():
    task = {"title": "Task 1"}
    prompt = PromptBuilder.test_fix_prompt(task, "test failure")
    assert "Task 1" in prompt
    assert "failed" in prompt


def test_review_fix_prompt():
    task = {"title": "Task 1"}
    prompt = PromptBuilder.review_fix_prompt(task, "fix this")
    assert "Task 1" in prompt
    assert "fix this" in prompt


def test_ci_fix_prompt():
    task = {"title": "Task 1"}
    prompt = PromptBuilder.ci_fix_prompt(task, "CI log")
    assert "Task 1" in prompt
    assert "CI" in prompt


def test_pr_body():
    task = {
        "title": "Task 1",
        "description": "Task description",
        "acceptance_criteria": ["AC1", "AC2"],
    }
    prompt = PromptBuilder.pr_body(task)
    assert "Task 1" in prompt
    assert "Task description" in prompt
    assert "[ ] AC1" in prompt
    assert "[ ] AC2" in prompt
