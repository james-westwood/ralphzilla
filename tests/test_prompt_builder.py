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

    prompt = PromptBuilder.coder_prompt(task, prd)
    assert "Task 1" in prompt
    assert "AC1" in prompt
    assert "1." in prompt
    assert "2." in prompt
    assert "file1.py" in prompt
    assert "Addendum 1" in prompt
    assert "Do NOT touch prd.json" in prompt


def test_coder_prompt_resume():
    task = {"title": "T1"}
    prompt = PromptBuilder.coder_prompt(task, {}, resume=True)
    assert "partial work" in prompt


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


def test_all_methods_are_staticmethods():
    """Verify core PromptBuilder methods are @staticmethod."""
    static_methods = [
        "coder_prompt",
        "reviewer_prompt",
        "test_writer_prompt",
        "decompose_prompt",
        "pr_body",
        "plan_check_prompt",
        "precommit_fix_prompt",
        "test_fix_prompt",
        "review_fix_prompt",
        "ci_fix_prompt",
        "review_quality_prompt",
        "test_quality_prompt",
        "planner_prompt",
        "critic_prompt",
        "prd_generate_prompt",
        "_inject_epic_addenda",
    ]
    for name in static_methods:
        attr = getattr(PromptBuilder, name, None)
        assert attr is not None, f"{name} not found"
        assert callable(attr), f"{name} is not callable"


def test_coder_prompt_includes_epic_addenda_when_matching():
    """Verify coder_prompt includes addenda when task epic matches prd."""
    task = {
        "title": "Task 1",
        "description": "Desc 1",
        "acceptance_criteria": ["AC1"],
        "files": ["file1.py"],
        "epic": "M2",
    }
    prd = {"epic_addenda": {"M2": "Check for edge case X in module Y."}}
    prompt = PromptBuilder.coder_prompt(task, prd)
    assert "Check for edge case X in module Y." in prompt
    assert "Epic-specific checks (M2)" in prompt


def test_coder_prompt_no_addenda_when_no_match():
    """Verify coder_prompt excludes addenda when epics don't match."""
    task = {
        "title": "Task 1",
        "description": "Desc 1",
        "acceptance_criteria": ["AC1"],
        "files": ["file1.py"],
        "epic": "M2",
    }
    prd = {"epic_addenda": {"M1": "Wrong epic addendum."}}
    prompt = PromptBuilder.coder_prompt(task, prd)
    assert "Wrong epic addendum" not in prompt


def test_reviewer_prompt_includes_all_six_review_categories():
    """Verify reviewer_prompt includes all 6 review categories."""
    task = {
        "title": "Code Review Task",
        "description": "Review implementation",
        "epic": "M1",
    }
    prd = {"epic_addenda": {}}
    prompt = PromptBuilder.reviewer_prompt(task, "diff content", prd, 1)
    assert "Correctness" in prompt
    assert "Security" in prompt
    assert "Performance" in prompt
    assert "Maintainability" in prompt
    assert "Testing" in prompt
    assert "PRD adherence" in prompt


def test_test_writer_prompt_demands_failing_tests():
    """Verify test_writer_prompt instructs failing tests only."""
    task = {
        "title": "Task 1",
        "description": "D1",
        "acceptance_criteria": ["AC1"],
    }
    prompt = PromptBuilder.test_writer_prompt(task)
    assert "failing tests only" in prompt.lower() or "fail" in prompt.lower()
    assert "ImportError or AssertionError" in prompt


def test_decompose_prompt_requests_json_list():
    """Verify decompose_prompt requests JSON list format."""
    task = {"title": "Complex Task"}
    prompt = PromptBuilder.decompose_prompt(task)
    assert "JSON list" in prompt or "JSON" in prompt


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
