# The model key switching (services/agent_api_keys, driven by scripts/manage.py's `keys use`): what a key set
# may hold, and how it's written into .env.<env> -- the old provider's lines all gone, everything else
# untouched, the file's permissions kept, nothing written on a dry run. Runs in a temp folder standing in for
# the repo root.

import os
import stat

import pytest

from scripts import manage
from src.services import agent_api_keys

_ENV_TEXT = """ENV=prod
# the model
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-ds-old
JWT_SECRET=keep-me
"""


def test_switching_drops_every_old_key_line_and_keeps_the_rest():
    text = agent_api_keys.to_env_text_with_key_set(_ENV_TEXT, {"ANTHROPIC_API_KEY": "sk-ant-new"})
    assert text == "ENV=prod\n# the model\nJWT_SECRET=keep-me\nANTHROPIC_API_KEY=sk-ant-new\n"


@pytest.mark.parametrize(
    ("key_set", "is_ok"),
    [
        ({"ANTHROPIC_API_KEY": "sk-ant"}, True),
        ({"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_AUTH_TOKEN": "t"}, True),
        ({"ANTHROPIC_AUTH_TOKEN": "t"}, False),  # a token with no endpoint would go to Anthropic
        ({"ANTHROPIC_API_KEY": ""}, False),
        ({"ANTHROPIC_API_KEY": "sk-ant", "JWT_SECRET": "x"}, False),  # a key set can't touch anything else
    ],
)
def test_what_a_key_set_may_hold(key_set, is_ok):
    assert (agent_api_keys.check_key_set(key_set) == []) == is_ok


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agent_api_keys").mkdir()
    (tmp_path / ".agent_api_keys" / "anthropic.env").write_text("ANTHROPIC_API_KEY=sk-ant-new\n")
    env_path = tmp_path / ".env.prod"
    env_path.write_text(_ENV_TEXT)
    os.chmod(env_path, 0o600)
    return tmp_path


def test_a_dry_run_writes_nothing(repo_root):
    manage._use_keys("anthropic", "prod", is_applying=False)
    assert (repo_root / ".env.prod").read_text() == _ENV_TEXT


def test_applying_writes_the_set_and_keeps_the_files_permissions(repo_root):
    manage._use_keys("anthropic", "prod", is_applying=True)
    env_path = repo_root / ".env.prod"
    assert agent_api_keys.read_env_file(env_path)["ANTHROPIC_API_KEY"] == "sk-ant-new"
    assert "ANTHROPIC_BASE_URL" not in agent_api_keys.read_env_file(env_path)
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert sorted(path.name for path in repo_root.iterdir()) == [".agent_api_keys", ".env.prod"]  # no temp left


def test_an_unknown_set_is_refused(repo_root):
    with pytest.raises(SystemExit, match="no key set"):
        manage._use_keys("nope", "prod", is_applying=True)
