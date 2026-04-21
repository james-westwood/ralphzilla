import pytest

from ralph import PlanInvalidError, PrdValidator


class TestPrdValidator:
    def test_validate_rule1_short_description_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "Short",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "description too short" in str(exc_info.value)

    def test_validate_rule2_no_file_path_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely "
            "longer than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": ["Must work correctly", "No file reference here"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "no acceptance criterion contains a file path pattern" in str(exc_info.value)

    def test_validate_rule3_credential_in_description_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that definitely contains "
            "a secret key for the API integration and must be 100 chars.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "contains credential string" in str(exc_info.value)

    def test_validate_rule3_credential_password_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that definitely uses "
            "password authentication for the service and must be 100 chars.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "contains credential string" in str(exc_info.value)

    def test_validate_rule3_credential_token_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that definitely uses "
            "a token for authentication and must be over 100 chars.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "contains credential string" in str(exc_info.value)

    def test_validate_rule3_credential_api_key_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that definitely uses "
            "an API key for the integration and must be over 100 chars.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "contains credential string" in str(exc_info.value)

    def test_validate_rule3_human_task_allows_credential(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that definitely uses "
            "a secret key for the service and must be over 100 chars.",
            "acceptance_criteria": ["Must configure credentials in tests/test_config.py"],
            "owner": "human",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        validator.validate(task, all_task_ids)

    def test_validate_rule4_unknown_depends_on_id_fails(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely longer "
            "than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": ["T2"],
        }
        all_task_ids = {"T1"}

        with pytest.raises(PlanInvalidError) as exc_info:
            validator.validate(task, all_task_ids)
        assert "depends_on unknown task 'T2'" in str(exc_info.value)

    def test_validate_valid_task_passes(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely "
            "longer than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": ["Must update tests/test_module.py"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        validator.validate(task, all_task_ids)

    def test_validate_valid_task_with_file_path_pattern_dot_py(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely "
            "longer than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": ["Update ralph.py to implement feature"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        validator.validate(task, all_task_ids)

    def test_validate_valid_task_with_tests_directory(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely "
            "longer than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": ["Add tests for the new feature in tests/"],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        validator.validate(task, all_task_ids)

    def test_validate_valid_task_with_multiple_acs(self):
        validator = PrdValidator()
        task = {
            "id": "T1",
            "description": "This is a valid task description that is definitely "
            "longer than one hundred characters to satisfy the prd validator rule.",
            "acceptance_criteria": [
                "Must update tests/test_module.py",
                "Should also update src/core.py",
            ],
            "owner": "ralph",
            "depends_on": [],
        }
        all_task_ids = {"T1"}

        validator.validate(task, all_task_ids)
