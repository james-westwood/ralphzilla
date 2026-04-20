from pathlib import Path

import pytest

from ralph import (
    AgentSandboxViolation,
    BranchExistsError,
    BranchStatus,
    BranchSyncError,
    CIFailedFatal,
    CIResult,
    CITimeoutError,
    CoderFailedError,
    Config,
    PlanCheckResult,
    PlanInvalidError,
    PRDGuardViolation,
    PreCommitResult,
    PreflightError,
    PRInfo,
    RalphError,
    RemoteNotSSHError,
    ReviewerFailedError,
    ReviewQualityResult,
    ReviewResult,
    TaskResult,
    TestQualityResult,
)


def test_exceptions_hierarchy():
    exceptions = [
        BranchSyncError,
        BranchExistsError,
        RemoteNotSSHError,
        CITimeoutError,
        CIFailedFatal,
        PRDGuardViolation,
        CoderFailedError,
        ReviewerFailedError,
        PreflightError,
        PlanInvalidError,
        AgentSandboxViolation,
    ]
    for exc in exceptions:
        assert issubclass(exc, RalphError)
        with pytest.raises(RalphError):
            raise exc("test error")


def test_dataclass_instantiation():
    # Config
    config = Config(
        max_iterations=10,
        skip_review=False,
        tdd_mode=True,
        model_mode="gemini",
        opencode_model="kimi",
        resume=True,
        repo_dir=Path("."),
        log_file=Path("ralph.log"),
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )
    assert config.tdd_mode is True

    # PRInfo
    pr = PRInfo(number=123, url="https://github.com/org/repo/pull/123")
    assert pr.number == 123

    # CIResult
    ci = CIResult(passed=True, rounds_used=1)
    assert ci.passed is True

    # ReviewResult
    review = ReviewResult(verdict="APPROVED", rounds_used=0)
    assert review.verdict == "APPROVED"

    # PreCommitResult
    pc = PreCommitResult(passed=True, rounds_used=0)
    assert pc.passed is True

    # BranchStatus
    bs = BranchStatus(existed=False, had_commits=False)
    assert bs.existed is False

    # TaskResult
    tr = TaskResult(fatal=False, message="Success")
    assert tr.fatal is False

    # PlanCheckResult
    pcr = PlanCheckResult(valid=True, errors=[], warnings=[], tasks_checked=5, decompositions=0)
    assert pcr.valid is True

    # TestQualityResult
    tqr = TestQualityResult(
        passed=True, hollow_tests=[], deterministic_issues=[], ai_issues=[], rounds_used=1
    )
    assert tqr.passed is True

    # ReviewQualityResult
    rqr = ReviewQualityResult(acceptable=True, reason="")
    assert rqr.acceptable is True
