from ralph import BlockerAnalyser, BlockerKind, BlockerResult


def test_classifies_merge_conflict_from_git_output():
    analyser = BlockerAnalyser()

    error_output = """merge branch 'main' into feature branch
Auto-merging ralph.py
CONFLICT (content): Merge conflict in ralph.py
Automatic merge failed; fix conflicts and then commit the result."""

    result = analyser.analyse(
        exit_code=1,
        error_output=error_output,
        task_id="M4-02",
    )

    assert result is not None
    assert result.kind == BlockerKind.MERGE_CONFLICT
    assert result.task_id == "M4-02"
    assert result.context


def test_classifies_ci_fatal_from_cifailedfatal_exception():
    analyser = BlockerAnalyser()

    error_output = "CI still failing after 2 fix rounds"

    result = analyser.analyse(
        exit_code=1,
        error_output=error_output,
        task_id="M4-02",
    )

    assert result is not None
    assert result.kind == BlockerKind.CI_FATAL
    assert result.task_id == "M4-02"
    assert "ci" in result.context.lower() or "fatal" in result.context.lower()


def test_classifies_prd_guard_from_prdguardviolation():
    analyser = BlockerAnalyser()

    error_output = "PRDGuardViolation: prd.json must not be modified"

    result = analyser.analyse(
        exit_code=1,
        error_output=error_output,
        task_id="M4-02",
    )

    assert result is not None
    assert result.kind == BlockerKind.PRD_GUARD_VIOLATION
    assert result.task_id == "M4-02"
    assert "prd" in result.context.lower() or "guard" in result.context.lower()


def test_classifies_reviewer_unavailable_from_empty_response():
    analyser = BlockerAnalyser()

    error_output = "Reviewer claude returned no output"

    result = analyser.analyse(
        exit_code=1,
        error_output=error_output,
        task_id="M4-02",
    )

    assert result is not None
    assert result.kind == BlockerKind.REVIEWER_UNAVAILABLE
    assert result.task_id == "M4-02"
    assert "reviewer" in result.context.lower() or "unavailable" in result.context.lower()


def test_analyse_returns_none_for_unknown_errors():
    analyser = BlockerAnalyser()

    error_output = "Some unknown error occurred"

    result = analyser.analyse(
        exit_code=1,
        error_output=error_output,
        task_id="M4-02",
    )

    assert result is None


def test_analyse_with_empty_error_output():
    analyser = BlockerAnalyser()

    result = analyser.analyse(exit_code=0, error_output="", task_id="M4-02")

    assert result is None


def test_multiple_conflict_patterns():
    analyser = BlockerAnalyser()

    patterns = [
        "merge conflict in file.py",
        "CONFLICT in src/main.rs",
        "Automatic merge failed",
    ]

    for pattern in patterns:
        result = analyser.analyse(exit_code=1, error_output=pattern, task_id="M1-01")
        assert result is not None
        assert result.kind == BlockerKind.MERGE_CONFLICT


def test_blocker_result_dataclass():
    result = BlockerResult(
        kind=BlockerKind.CI_FATAL,
        task_id="M4-02",
        context="CI failed after max fix rounds",
    )

    assert result.kind == BlockerKind.CI_FATAL
    assert result.task_id == "M4-02"
    assert result.context == "CI failed after max fix rounds"
