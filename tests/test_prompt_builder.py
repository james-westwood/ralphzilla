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
    assert "file1.py" in prompt
    assert "Addendum 1" in prompt
    assert "Do NOT touch prd.json" in prompt


def test_coder_prompt_resume():
    task = {"title": "T1"}
    prompt = PromptBuilder.coder_prompt(task, "C", {}, resume=True)
    assert "already has commits" in prompt


def test_reviewer_prompt():
    task = {"title": "T1", "description": "D1"}
    prompt = PromptBuilder.reviewer_prompt(task, "diff", {}, 1)
    assert "Correctness" in prompt
    assert "APPROVED" in prompt
    assert "CHANGES REQUESTED" in prompt


def test_test_writer_prompt():
    task = {"title": "T1", "description": "D1", "acceptance_criteria": ["AC1"]}
    prompt = PromptBuilder.test_writer_prompt(task)
    assert "Write failing tests only" in prompt
    assert "Do NOT implement the module" in prompt


def test_decompose_prompt():
    task = {"title": "Big Task"}
    prompt = PromptBuilder.decompose_prompt(task)
    assert "Break the following complexity-3 task" in prompt
    assert "JSON list" in prompt
