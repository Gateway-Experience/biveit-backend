# Intentions: move existing data onto the stages' new names -- the website creation process is Describing ->
# Planning -> Building -> Launching now, where it used to be spec-ing -> implementing. Two places hold the
# old names, and this brings both over. Dry run by default; safe to re-run (what's done stays done).
#
#   uv run python -m scripts.rename_stages [--apply]
#
# The database:
#   projects.stage              specing / implementing -> planning / building (and the column's default)
#   projects.implementation_done_at -> build_approved_at
#   ai_messages.content         signal slugs IMPLEMENTATION_STARTED / _DONE -> PLAN_APPROVED / BUILD_APPROVED, and
#                               approval actions APPROVE_SPEC / _IMPLEMENTATION -> APPROVE_PLAN / APPROVE_BUILD
#                               (the building status, BUILDING / BUILDING_DONE, keeps its name)
# Each workspace's CLAUDE.md, the playbook copied in from data/template when it was seeded: the agent rereads it
# every session, so an old copy would keep telling it to ask for APPROVE_SPEC. A copy that's exactly one of the
# template's past versions (from this repo's git history) is replaced by the current template; one the agent
# changed only gets the old names swapped, so its changes stay. Read and written through utils/paths (the
# folder is the agent's), so a stopped container doesn't matter. Claude Code's transcripts are left alone:
# they're history, and the session's current tools and instructions are what it follows.

import argparse
import asyncio
import functools
import re
import subprocess
from pathlib import Path

from src.services import devenv
from src.utils import paths, postgres

_TEMPLATE_PATH = "data/template/CLAUDE.md"
# The quoted tokens renamed inside stored messages (exact JSON strings, so nothing else can match).
_MESSAGE_TOKENS = {
    "IMPLEMENTATION_STARTED": "PLAN_APPROVED",
    "IMPLEMENTATION_DONE": "BUILD_APPROVED",
    "APPROVE_SPEC": "APPROVE_PLAN",
    "APPROVE_IMPLEMENTATION": "APPROVE_BUILD",
}
# For a playbook copy the agent changed: the old names, swapped where they stand.
_PLAYBOOK_RENAMES = [
    (r"\bAPPROVE_SPEC\b", "APPROVE_PLAN"),
    (r"\bAPPROVE_IMPLEMENTATION\b", "APPROVE_BUILD"),
    (r"The Building Process", "The Website Creation Process"),
    (r"building process", "website creation process"),
    (r"(?<![A-Za-z])Spec-ing(?![A-Za-z])", "Planning"),
    (r"(?<![A-Za-z])spec-ing(?![A-Za-z])", "planning"),
    (r"(?<![A-Za-z])Implementing(?![A-Za-z])", "Building"),
    (r"(?<![A-Za-z])implementing(?![A-Za-z])", "building"),
]


async def _migrate_database(is_applying: bool) -> None:
    has_old_column = await postgres.fetchrow(
        "SELECT 1 AS found FROM information_schema.columns WHERE table_name = 'projects' AND column_name = 'implementation_done_at'"
    )
    stages = await postgres.fetch(
        "SELECT stage, count(*) AS n FROM projects WHERE stage IN ('specing', 'implementing') GROUP BY stage"
    )
    token_pattern = "|".join(f'"{token}"' for token in _MESSAGE_TOKENS)
    message_count = (
        await postgres.fetchrow(
            "SELECT count(*) AS n FROM ai_messages WHERE content::text ~ $pattern", {"pattern": token_pattern}
        )
    )["n"]
    print(
        "database:\n"
        + f"  projects.implementation_done_at -> build_approved_at: {'to rename' if has_old_column else 'done'}\n"
        + f"  projects.stage to rename: {', '.join(f'{row["stage"]} x{row["n"]}' for row in stages) or 'none'}\n"
        + f"  ai_messages with old signal/action names: {message_count}"
    )
    if not is_applying:
        return
    if has_old_column:
        await postgres.execute("ALTER TABLE projects RENAME COLUMN implementation_done_at TO build_approved_at")
    await postgres.execute("ALTER TABLE projects ALTER COLUMN stage SET DEFAULT 'planning'")
    await postgres.execute(
        "UPDATE projects SET stage = CASE stage WHEN 'specing' THEN 'planning' ELSE 'building' END "
        + "WHERE stage IN ('specing', 'implementing')"
    )
    replaced = "content::text"
    for old, new in _MESSAGE_TOKENS.items():
        replaced = f"replace({replaced}, '\"{old}\"', '\"{new}\"')"
    await postgres.execute(
        f"UPDATE ai_messages SET content = ({replaced})::jsonb WHERE content::text ~ $pattern",
        {"pattern": token_pattern},
    )


def _get_template_versions() -> set[bytes]:
    """[Pure-ish] Every version the playbook template has had, from this repo's git history -- a workspace copy
    that's byte-for-byte one of them is the agent's untouched copy. Empty without git."""
    try:
        commits = subprocess.check_output(["git", "log", "--format=%H", "--", _TEMPLATE_PATH], text=True).split()
        return {subprocess.check_output(["git", "show", f"{commit}:{_TEMPLATE_PATH}"]) for commit in commits}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()


def to_renamed_playbook(text: str) -> str:
    """[Pure] A playbook copy with the old stage names and approval actions swapped for the new ones."""
    return functools.reduce(lambda renamed, rule: re.sub(rule[0], rule[1], renamed), _PLAYBOOK_RENAMES, text)


def migrate_playbook(
    project_dir: str, relative_path: str, current_template: bytes, old_versions: set[bytes], is_applying: bool
) -> str | None:
    """Bring one workspace's playbook copy over (see the top of this file), through utils/paths only. Returns
    what it did (or would do), or None when there's nothing to do: no copy, or already current."""
    try:
        playbook = paths.read_within(project_dir, relative_path)
    except (FileNotFoundError, NotADirectoryError):
        return None  # never seeded
    except paths.UnsafePath:
        return "a symlink (or behind one): skipped, not followed"
    if playbook in old_versions:
        new_playbook, how = current_template, "untouched old copy -> the current template"
    else:
        new_playbook, how = to_renamed_playbook(playbook.decode()).encode(), "changed copy -> old names swapped"
    if new_playbook == playbook:
        return None
    if is_applying:
        paths.replace_file_within(project_dir, relative_path, new_playbook)
    return how


async def _migrate_playbooks(is_applying: bool) -> None:
    current_template = await asyncio.to_thread(Path(_TEMPLATE_PATH).read_bytes)  # ours, not a project's
    old_versions = _get_template_versions() - {current_template}
    workspaces = await postgres.fetch(
        """
        SELECT users.hash AS user_hash, projects.hash AS project_hash, ai_conversations.id AS conversation_id
        FROM ai_conversations
        JOIN projects ON projects.id = ai_conversations.project_id AND projects.deleted_at IS NULL
        JOIN users ON users.id = projects.user_id
        WHERE ai_conversations.deleted_at IS NULL
        """
    )
    print(f"workspace playbooks ({len(workspaces)} workspaces):")
    results = [
        (
            workspace["conversation_id"],
            await asyncio.to_thread(
                migrate_playbook,
                devenv.get_project_dir({"user_hash": workspace["user_hash"], "hash": workspace["project_hash"]}),
                f"{workspace['conversation_id']}/CLAUDE.md",
                current_template,
                old_versions,
                is_applying,
            ),
        )
        for workspace in workspaces
    ]
    print(
        "\n".join(f"  conversation {conversation_id}: {how}" for conversation_id, how in results if how)
        or "  all current"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Move existing data onto the stages' new names.")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args()

    await postgres.connect()
    try:
        await _migrate_database(args.apply)
        await _migrate_playbooks(args.apply)
    finally:
        await postgres.close()
    print("applied." if args.apply else "dry run: add --apply to make these changes.")


if __name__ == "__main__":
    asyncio.run(main())
