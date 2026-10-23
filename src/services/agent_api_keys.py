# Intentions: the model key sets -- .env-format files in AGENT_API_KEYS_DIR (at the repo root), one per set,
# each holding only AGENT_API_KEY_NAMES -- and switching .env.<env> over to one. scripts/manage.py's `keys`
# drives the switching; platform_status names the set in use.
#
# Applying a set rewrites EVERY AGENT_API_KEY_NAMES line in the env file, so nothing of the previous
# provider lingers: a base URL left behind would send the new key to the old endpoint. The backend reads the
# keys at start, so it needs a restart afterwards.

import os
import re
import stat
from pathlib import Path

from dotenv import dotenv_values

from src.constants import AGENT_API_KEY_NAMES, AGENT_API_KEYS_DIR


def read_env_file(path: Path) -> dict[str, str]:
    """[Pure-ish] An .env-format file's variables (a bare `NAME` counts as empty), or none if there's no file."""
    return {name: value or "" for name, value in dotenv_values(path).items()} if path.is_file() else {}


def find_key_sets() -> dict[str, dict[str, str]]:
    """[Pure-ish] The key sets in AGENT_API_KEYS_DIR by name (the file name, minus a .env suffix), each as its
    variables. Hidden files (an editor's swap file...) are skipped."""
    folder = Path(AGENT_API_KEYS_DIR)
    paths = (
        sorted(path for path in folder.iterdir() if path.is_file() and not path.name.startswith("."))
        if folder.is_dir()
        else []
    )
    return {path.name.removesuffix(".env"): read_env_file(path) for path in paths}


def check_key_set(key_set: dict[str, str]) -> list[str]:
    """[Pure] What's wrong with a key set, if anything: only AGENT_API_KEY_NAMES, nothing empty, and a key --
    Anthropic's API key, or another endpoint's URL and token together."""
    unknown = [name for name in key_set if name not in AGENT_API_KEY_NAMES]
    empty = [name for name, value in key_set.items() if not value]
    has_key = bool(key_set.get("ANTHROPIC_API_KEY")) or (
        bool(key_set.get("ANTHROPIC_BASE_URL")) and bool(key_set.get("ANTHROPIC_AUTH_TOKEN"))
    )
    return [
        *[f"{name} isn't a model key variable (only {', '.join(AGENT_API_KEY_NAMES)})" for name in unknown],
        *[f"{name} is empty" for name in empty],
        *([] if has_key else ["needs ANTHROPIC_API_KEY, or ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN"]),
    ]


def to_env_text_with_key_set(env_text: str, key_set: dict[str, str]) -> str:
    """[Pure] An env file's text with its model keys replaced by key_set's: every AGENT_API_KEY_NAMES line
    goes, the set's lines are appended, everything else stays as it was."""
    key_line = re.compile(rf"^\s*(export\s+)?({'|'.join(AGENT_API_KEY_NAMES)})\s*=")
    kept_lines = [line for line in env_text.splitlines() if not key_line.match(line)]
    return "\n".join([*kept_lines, *[f"{name}={value}" for name, value in key_set.items()]]) + "\n"


def describe_key_set(key_set: dict[str, str]) -> str:
    """[Pure] A key set for showing: the base URL as is, keys down to their last 4 characters."""
    return (
        ", ".join(
            f"{name}={value if name == 'ANTHROPIC_BASE_URL' or not value else f'...{value[-4:]}'}"
            for name, value in key_set.items()
        )
        or "nothing"
    )


def describe_current_key_set(current_keys: dict[str, str]) -> str:
    """[Pure-ish] Which key set the given key variables are (missing = empty), for showing."""
    set_keys = {name: current_keys.get(name) or "" for name in AGENT_API_KEY_NAMES}
    matching = [
        name
        for name, key_set in find_key_sets().items()
        if set_keys == {key: key_set.get(key, "") for key in AGENT_API_KEY_NAMES}
    ]
    if not any(set_keys.values()):
        return "NONE SET -- agents can't start"
    described = describe_key_set({name: value for name, value in set_keys.items() if value})
    return f"{matching[0]} ({described})" if matching else f"not one of {AGENT_API_KEYS_DIR}/ ({described})"


def write_env_file(path: Path, text: str) -> None:
    """Replace the env file in one go, keeping its permissions: a temp file created with the original's mode
    (so the secrets in it are never more readable than before), then renamed over it."""
    temp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(path.stat().st_mode))
    with os.fdopen(fd, "w") as temp_file:
        temp_file.write(text)
    os.replace(temp_path, path)
