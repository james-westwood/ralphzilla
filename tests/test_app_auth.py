import os
import time
from unittest.mock import MagicMock, patch

from ralph import BOT_APP_ID, BOT_EMAIL, BOT_NAME, BOT_USER_ID, AppAuth, SubprocessRunner


class TestAppAuthGitEnv:
    def test_git_env_returns_bot_identity(self):
        auth = AppAuth(app_id=BOT_APP_ID, private_key_pem="dummy", install_id=123)
        env = auth.git_env()
        assert env["GIT_AUTHOR_NAME"] == BOT_NAME
        assert env["GIT_AUTHOR_EMAIL"] == BOT_EMAIL
        assert env["GIT_COMMITTER_NAME"] == BOT_NAME
        assert env["GIT_COMMITTER_EMAIL"] == BOT_EMAIL

    def test_git_env_email_contains_bot_user_id(self):
        auth = AppAuth(app_id=BOT_APP_ID, private_key_pem="dummy", install_id=123)
        env = auth.git_env()
        assert str(BOT_USER_ID) in env["GIT_AUTHOR_EMAIL"]
        assert "ralphzilla[bot]" in env["GIT_AUTHOR_EMAIL"]


class TestAppAuthGhEnv:
    def test_gh_env_returns_token_key(self):
        auth = AppAuth(app_id=BOT_APP_ID, private_key_pem="dummy", install_id=123)
        auth._token = "ghs_test_token"
        auth._token_expires = time.time() + 3600
        env = auth.gh_env()
        assert "GH_TOKEN" in env
        assert env["GH_TOKEN"] == "ghs_test_token"


class TestAppAuthCreate:
    def test_create_returns_none_when_key_missing(self, tmp_path):
        result = AppAuth.create(key_path=tmp_path / "nonexistent.pem")
        assert result is None

    def test_create_returns_auth_when_key_exists(self, tmp_path):
        key_file = tmp_path / "test-key.pem"
        key_file.write_text("dummy-pem-content")
        with patch.object(AppAuth, "_resolve_install_id", return_value=42):
            auth = AppAuth.create(key_path=key_file)
        assert auth is not None
        assert auth._install_id == 42

    def test_create_returns_none_when_install_id_unavailable(self, tmp_path):
        key_file = tmp_path / "test-key.pem"
        key_file.write_text("dummy-pem-content")
        with patch.object(AppAuth, "_resolve_install_id", return_value=None):
            auth = AppAuth.create(key_path=key_file)
        assert auth is None


class TestAppAuthTokenRefresh:
    def test_cached_token_reused_before_expiry(self):
        auth = AppAuth(app_id=BOT_APP_ID, private_key_pem="dummy", install_id=123)
        auth._token = "ghs_cached"
        auth._token_expires = time.time() + 3600
        assert auth._ensure_token() == "ghs_cached"

    def test_token_refreshed_near_expiry(self):
        auth = AppAuth(app_id=BOT_APP_ID, private_key_pem="dummy", install_id=123)
        auth._token = "ghs_old"
        auth._token_expires = time.time() + 100
        with patch.object(auth, "_ensure_token", return_value="ghs_new"):
            env = auth.gh_env()
        assert env["GH_TOKEN"] == "ghs_new"


class TestSubprocessRunnerEnvAdditions:
    def test_env_additions_merged_into_subprocess(self, tmp_path):
        logger = MagicMock()
        runner = SubprocessRunner(logger)
        script = tmp_path / "print_env.py"
        script.write_text("import os; print(os.environ.get('RALPH_TEST_VAR', 'MISSING'))")
        result = runner.run(
            ["python3", str(script)],
            env_additions={"RALPH_TEST_VAR": "hello_from_test"},
            check=True,
        )
        assert "hello_from_test" in result.stdout

    def test_env_additions_override_existing(self, tmp_path):
        logger = MagicMock()
        runner = SubprocessRunner(logger)
        script = tmp_path / "print_path.py"
        script.write_text("import os; print(os.environ.get('MY_TEST_RALPH_VAR', 'MISSING'))")
        os.environ["MY_TEST_RALPH_VAR"] = "original"
        try:
            result = runner.run(
                ["python3", str(script)],
                env_additions={"MY_TEST_RALPH_VAR": "overridden"},
                check=True,
            )
        finally:
            del os.environ["MY_TEST_RALPH_VAR"]
        assert "overridden" in result.stdout

    def test_env_additions_none_is_noop(self):
        logger = MagicMock()
        runner = SubprocessRunner(logger)
        result = runner.run(["echo", "test"], env_additions=None, check=True)
        assert result.stdout.strip() == "test"

    def test_env_additions_empty_dict_is_noop(self):
        logger = MagicMock()
        runner = SubprocessRunner(logger)
        result = runner.run(["echo", "test"], env_additions={}, check=True)
        assert result.stdout.strip() == "test"


class TestBotConstants:
    def test_bot_name_format(self):
        assert BOT_NAME == "ralphzilla[bot]"

    def test_bot_email_format(self):
        assert f"{BOT_USER_ID}+ralphzilla[bot]@users.noreply.github.com" == BOT_EMAIL

    def test_bot_app_id_is_string(self):
        assert isinstance(BOT_APP_ID, str)
