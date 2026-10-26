# scripts/rename_stages.py's playbook step: an untouched old copy of the template becomes the current one, a copy
# the agent changed only gets the old names swapped, and a planted symlink is never followed or written through.

import os

from scripts import rename_stages

_OLD = b"Ask them to approve the spec (the APPROVE_SPEC approval). ## Implementing\n"
_CURRENT = b"Ask them to approve the plan (the APPROVE_PLAN approval). ## Building\n"


def test_an_untouched_old_copy_becomes_the_current_template(tmp_path):
    (tmp_path / "5").mkdir()
    (tmp_path / "5" / "CLAUDE.md").write_bytes(_OLD)
    assert rename_stages.migrate_playbook(str(tmp_path), "5/CLAUDE.md", _CURRENT, {_OLD}, is_applying=False)
    assert (tmp_path / "5" / "CLAUDE.md").read_bytes() == _OLD  # a dry run writes nothing
    rename_stages.migrate_playbook(str(tmp_path), "5/CLAUDE.md", _CURRENT, {_OLD}, is_applying=True)
    assert (tmp_path / "5" / "CLAUDE.md").read_bytes() == _CURRENT


def test_a_changed_copy_keeps_its_changes(tmp_path):
    (tmp_path / "5").mkdir()
    (tmp_path / "5" / "CLAUDE.md").write_bytes(b"My own note. Use APPROVE_SPEC while spec-ing, then Implementing.\n")
    rename_stages.migrate_playbook(str(tmp_path), "5/CLAUDE.md", _CURRENT, {_OLD}, is_applying=True)
    assert (
        tmp_path / "5" / "CLAUDE.md"
    ).read_bytes() == b"My own note. Use APPROVE_PLAN while planning, then Building.\n"


def test_a_planted_symlink_is_never_followed(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_bytes(_OLD)
    project = tmp_path / "project"
    (project / "5").mkdir(parents=True)
    os.symlink(outside, project / "5" / "CLAUDE.md")
    assert "symlink" in rename_stages.migrate_playbook(str(project), "5/CLAUDE.md", _CURRENT, {_OLD}, is_applying=True)
    assert outside.read_bytes() == _OLD  # the target outside the project is untouched
