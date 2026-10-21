# utils/paths tests: file access inside a folder we don't trust. Real files in tmp_path -- the whole point
# is how the filesystem treats symlinks, so faking it would test nothing. `root` plays a project folder;
# `outside` is what a planted symlink would try to reach (the backend's .env, say).

import os

import pytest

from src.utils import paths


@pytest.fixture
def root(tmp_path):
    (tmp_path / "root/sub").mkdir(parents=True)
    (tmp_path / "root/sub/file.txt").write_text("inside")
    return tmp_path / "root"


@pytest.fixture
def outside(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/secret.txt").write_text("platform secret")
    return tmp_path / "outside"


def test_plain_paths_work(root):
    assert paths.read_within(str(root), "sub/file.txt") == b"inside"
    assert paths.read_within(str(root), "./sub//file.txt") == b"inside"
    assert paths.lstat_within(str(root), "sub/nope") is None
    assert paths.lstat_within(str(root), "nope/deeper") is None
    assert [name for name, _ in paths.list_within(str(root), "sub")] == ["file.txt"]


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../outside/secret.txt", "sub/../../outside/secret.txt"])
@pytest.mark.usefixtures("outside")
def test_absolute_paths_and_dotdot_are_refused(root, bad_path):
    with pytest.raises(paths.UnsafePath):
        paths.read_within(str(root), bad_path)


@pytest.mark.parametrize("link_target", ["absolute", "relative"])
def test_a_symlinked_file_is_never_read(root, outside, link_target):
    (root / "link.txt").symlink_to(outside / "secret.txt" if link_target == "absolute" else "../outside/secret.txt")
    with pytest.raises(paths.UnsafePath):
        paths.read_within(str(root), "link.txt")


def test_a_symlinked_folder_on_the_way_is_refused(root, outside):
    (root / "linked").symlink_to(outside)
    with pytest.raises(paths.UnsafePath):
        paths.read_within(str(root), "linked/secret.txt")
    with pytest.raises(paths.UnsafePath):
        paths.lstat_within(str(root), "linked/secret.txt")
    with pytest.raises(paths.UnsafePath):
        paths.list_within(str(root), "linked")
    with pytest.raises(paths.UnsafePath):
        paths.replace_file_within(str(root), "linked/new.txt", b"x")
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.txt"]


def test_symlinks_are_listed_as_themselves(root, outside):
    (root / "sub/link").symlink_to(outside / "secret.txt")
    listed = dict(paths.list_within(str(root), "sub"))
    assert os.path.islink(root / "sub/link") and listed["link"].st_size != len("platform secret")


def test_replacing_a_planted_symlink_replaces_the_link_not_its_target(root, outside):
    (root / "sub/config").symlink_to(outside / "secret.txt")
    paths.replace_file_within(str(root), "sub/config", b"ours", 0o600)
    assert (outside / "secret.txt").read_text() == "platform secret"
    assert not os.path.islink(root / "sub/config") and (root / "sub/config").read_bytes() == b"ours"
    assert oct((root / "sub/config").stat().st_mode & 0o777) == "0o600"
    assert sorted(entry.name for entry in (root / "sub").iterdir()) == ["config", "file.txt"]  # no temp left


def test_writing_creates_missing_folders(root):
    paths.replace_file_within(str(root), "new/deeper/file.txt", b"x")
    assert (root / "new/deeper/file.txt").read_bytes() == b"x"


def test_removing_a_tree_never_follows_a_link_in_it(root, outside):
    (root / "doomed/inner").mkdir(parents=True)
    (root / "doomed/link").symlink_to(outside)
    paths.remove_tree_within(str(root), "doomed")
    assert not (root / "doomed").exists() and (outside / "secret.txt").exists()
    paths.remove_tree_within(str(root), "doomed")  # gone already: fine


def test_removing_through_a_symlinked_folder_is_refused(root, outside):
    (root / "linked").symlink_to(outside)
    with pytest.raises(paths.UnsafePath):
        paths.remove_tree_within(str(root), "linked/secret.txt")
    assert (outside / "secret.txt").exists()


def test_copying_skips_what_is_there_and_never_writes_through_a_link(root, outside, tmp_path):
    source = tmp_path / "source"
    (source / "keep").mkdir(parents=True)
    (source / "keep/new.txt").write_text("from source")
    (source / "linked").mkdir()
    (source / "linked/x.txt").write_text("from source")
    (source / "skipme").write_text("ignored")
    (root / "dest/keep").mkdir(parents=True)
    (root / "dest/keep/new.txt").write_text("edited")
    (root / "dest/linked").symlink_to(outside)  # planted where a folder of ours goes

    paths.copy_tree_within(str(source), str(root), "dest", skip_existing=True, ignored_names=("skipme",))

    assert (root / "dest/keep/new.txt").read_text() == "edited"
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.txt"]
    assert not (root / "dest/skipme").exists()
    (root / "fresh").mkdir()
    (root / "fresh/linked").symlink_to(outside)
    with pytest.raises(paths.UnsafePath):  # without skip_existing, a link in the way is refused instead
        paths.copy_tree_within(str(source), str(root), "fresh", skip_existing=False)
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.txt"]


def test_a_non_regular_file_is_never_opened_for_reading(root):
    os.mkfifo(root / "pipe")
    with pytest.raises(paths.UnsafePath):
        paths.read_within(str(root), "pipe")
