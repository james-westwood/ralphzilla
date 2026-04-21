from unittest.mock import MagicMock

import pytest

from ralph import (
    PLAN_CONSENSUS_OUTPUT,
    Config,
    PlanConsensus,
    PromptBuilder,
)


@pytest.fixture
def mock_ai_runner():
    runner = MagicMock()
    logger = MagicMock()
    config = MagicMock()
    return runner, logger, config


@pytest.fixture
def temp_repo_dir(tmp_path):
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    return repo_dir


@pytest.fixture
def plan_config(temp_repo_dir, tmp_path):
    log_file = temp_repo_dir / "ralph.log"
    return Config(
        max_iterations=1,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model="opencode/kimi-k2.5",
        resume=False,
        repo_dir=temp_repo_dir,
        log_file=log_file,
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )


class TestPromptBuilderPlannerCritic:
    def test_planner_prompt_without_feedback(self):
        brief = "Build a user authentication system"
        prompt = PromptBuilder.planner_prompt(brief)

        assert "Build a user authentication system" in prompt
        assert "json" in prompt.lower()
        assert "title" in prompt.lower()
        assert "acceptance_criteria" in prompt.lower()

    def test_planner_prompt_with_feedback(self):
        brief = "Build a user authentication system"
        feedback = "Add specific acceptance criteria for password reset"
        prompt = PromptBuilder.planner_prompt(brief, feedback)

        assert brief in prompt
        assert feedback in prompt
        assert "Prior Critic Feedback" in prompt

    def test_critic_prompt(self):
        plan = '[{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC1"]}]'
        prompt = PromptBuilder.critic_prompt(plan)

        assert "REJECT" in prompt
        assert "OKAY" in prompt
        assert "vague language" in prompt.lower()


class TestPlanConsensusInit:
    def test_constructor(self, mock_ai_runner, plan_config):
        ai_runner, logger, config = mock_ai_runner
        consensus = PlanConsensus(ai_runner, logger, config)

        assert consensus.ai_runner is ai_runner
        assert consensus.logger is logger
        assert consensus.config is config


class TestPlanConsensusRun:
    def test_okay_exit_early(self, temp_repo_dir, plan_config):
        mock_logger = MagicMock(spec=["info"])
        mock_config = plan_config

        mock_ai_runner = MagicMock()
        mock_ai_runner.run_reviewer = MagicMock(
            side_effect=[
                '[{"title": "Task 1", "description": "Desc", '
                '"acceptance_criteria": ["AC1"], "owner": "ralph", '
                '"depends_on": []}]',
                "OKAY",
            ]
        )

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)
        consensus.run("Build auth system", max_iterations=3)

        assert mock_ai_runner.run_reviewer.call_count == 2

    def test_reject_loops_correctly(self, temp_repo_dir, plan_config):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = plan_config

        call_count = 0

        def fake_run_reviewer(agent, prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '[{"title": "Task 1"}]'
            elif call_count == 2:
                return "REJECT\n- Task 1: lacks measurable ACs"
            return "OKAY"

        mock_ai_runner.run_reviewer = fake_run_reviewer

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)
        consensus.run("Build auth", max_iterations=3)

        assert call_count == 4

    def test_max_iterations_respected(self, temp_repo_dir, plan_config):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = plan_config

        call_count = 0

        def fake_run_reviewer(agent, prompt):
            nonlocal call_count
            call_count += 1
            return '[{"title": "Task 1"}]'

        mock_ai_runner.run_reviewer = fake_run_reviewer

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)
        consensus.run("Build auth", max_iterations=2)

        assert call_count == 2

    def test_plan_written_to_file(self, temp_repo_dir, plan_config):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = plan_config

        def fake_run_reviewer(agent, prompt):
            return "OKAY"

        mock_ai_runner.run_reviewer = fake_run_reviewer

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)
        consensus.run("Build auth", max_iterations=1)

        output_path = temp_repo_dir / PLAN_CONSENSUS_OUTPUT
        assert output_path.exists()

    def test_parse_critic_reject_precedence(self):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = MagicMock()

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)

        verdict, reason = consensus._parse_critic("Some analysis\nOKAY\nREJECT\n- Issue")
        assert verdict == "REJECT"

    def test_parse_critic_unclear_treated_as_okay(self):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = MagicMock()

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)

        verdict, reason = consensus._parse_critic("Some random text without verdict")

        assert verdict == "OKAY"
        mock_logger.warn.assert_called()


class TestPlanConsensusOutput:
    def test_format_plan_creates_markdown(self):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = MagicMock()

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)

        plan = (
            '[{"title": "Task 1", "description": "Desc", '
            '"acceptance_criteria": ["AC1"], "owner": "ralph", "depends_on": []}]'
        )
        result = consensus._format_plan(plan, 2, "OKAY")

        assert "Work Plan" in result
        assert "Iterations" in result
        assert "Verdict" in result

    def test_render_markdown_tasks(self):
        mock_ai_runner = MagicMock()
        mock_logger = MagicMock()
        mock_config = MagicMock()

        consensus = PlanConsensus(mock_ai_runner, mock_logger, mock_config)

        tasks = [
            {
                "title": "Task 1",
                "description": "Description",
                "acceptance_criteria": ["AC1"],
                "owner": "ralph",
                "depends_on": [],
            }
        ]
        result = consensus._render_markdown_tasks(tasks)

        assert "### 1. Task 1" in result
        assert "**Owner**: ralph" in result
        assert "**Acceptance Criteria**:" in result
        assert "- AC1" in result
