import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from dropbox.files import FileMetadata, FolderMetadata

import restore


def dropbox_hash(data: bytes) -> str:
    """Dropbox's content_hash for data under 4 MB: one block."""
    return hashlib.sha256(hashlib.sha256(data).digest() if data else b"").hexdigest()


class FakeDropbox:
    """Just enough of the Dropbox client for restore: files by path, listed two entries per page."""

    def __init__(self, files):
        self.files = dict(files)
        self.listed, self.downloads, self.pages, self.fail = [], [], {}, set()

    def files_list_folder(self, path):
        self.listed.append(path)
        entries = {}
        for file_path, data in self.files.items():
            if file_path.startswith(path + "/"):
                name, _, rest = file_path[len(path) + 1:].partition("/")
                entries[name] = FolderMetadata(name=name, id=f"id:{name}") if rest else FileMetadata(
                    name=name, id=f"id:{name}", size=len(data), rev="0123456789", content_hash=dropbox_hash(data),
                    client_modified=datetime(2026, 1, 1), server_modified=datetime(2026, 1, 1))
        return self.page(list(entries.values()), 0)

    def page(self, entries, start):
        cursor = f"c{len(self.pages)}"
        self.pages[cursor] = (entries, start + 2)
        return SimpleNamespace(entries=entries[start:start + 2], cursor=cursor, has_more=start + 2 < len(entries))

    def files_list_folder_continue(self, cursor):
        return self.page(*self.pages[cursor])

    def files_download_to_file(self, download_path, path):
        self.downloads.append(path)
        if path in self.fail:
            self.fail.discard(path)
            raise ConnectionError("network down")
        with open(download_path, "wb") as f:
            f.write(self.files[path])


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, "WATCH_FOLDER", str(tmp_path / "restored"))
    monkeypatch.setattr(restore, "DROPBOX_FOLDER", "/Shared/Team/")
    monkeypatch.setattr(restore, "HOOK", tmp_path / "no-hook.sh")
    return tmp_path / "restored"


TEAM = {
    "/Shared/Team/a.txt": b"A",
    "/Shared/Team/b.txt": b"B",
    "/Shared/Team/c.txt": b"C",
    "/Shared/Team/Sub/d.txt": b"D",
    "/Shared/Team/Sub/Deeper/e.bin": b"E",
    "/Shared/Other/x.txt": b"not ours",
}


def test_content_hash_matches_dropbox_algorithm(tmp_path):
    (tmp_path / "empty").write_bytes(b"")
    assert restore.content_hash(tmp_path / "empty") == hashlib.sha256(b"").hexdigest()

    block = 4 * 1024 * 1024
    data = b"x" * block + b"y"
    (tmp_path / "two-blocks").write_bytes(data)
    expected = hashlib.sha256(hashlib.sha256(data[:block]).digest() + hashlib.sha256(b"y").digest()).hexdigest()
    assert restore.content_hash(tmp_path / "two-blocks") == expected


def test_restore_copies_the_whole_tree(local):
    dbx = FakeDropbox(TEAM)
    restore.sync_down(dbx)

    files = sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file())
    assert files == [".restore-state.json", "Sub/Deeper/e.bin", "Sub/d.txt", "a.txt", "b.txt", "c.txt"]
    assert (local / "Sub" / "Deeper" / "e.bin").read_bytes() == b"E"
    assert "/Shared/Team/Sub/Deeper/e.bin" in dbx.downloads
    assert json.loads((local / ".restore-state.json").read_text())["Sub/d.txt"] == dropbox_hash(b"D")


def test_later_passes_download_only_what_changed(local):
    dbx = FakeDropbox(TEAM)
    restore.sync_down(dbx)
    dbx.downloads.clear()

    restore.sync_down(dbx)
    assert dbx.downloads == []

    dbx.files["/Shared/Team/Sub/d.txt"] = b"D2"
    (local / "a.txt").unlink()
    restore.sync_down(dbx)
    assert sorted(dbx.downloads) == ["/Shared/Team/Sub/d.txt", "/Shared/Team/a.txt"]
    assert (local / "Sub" / "d.txt").read_bytes() == b"D2"


def test_identical_local_files_are_not_downloaded(local):
    local.mkdir()
    (local / "a.txt").write_bytes(b"A")
    (local / "b.txt").write_bytes(b"old B")
    dbx = FakeDropbox(TEAM)

    restore.sync_down(dbx)

    assert "/Shared/Team/a.txt" not in dbx.downloads
    assert (local / "b.txt").read_bytes() == b"B"


def test_hook_runs_once_and_its_edits_are_kept(local, tmp_path, monkeypatch):
    hook = tmp_path / "post-download-hook.sh"
    hook.write_text('echo "edited by hook" >> "$1"\n')
    monkeypatch.setattr(restore, "HOOK", hook)
    dbx = FakeDropbox({"/Shared/Team/dump.sql": b"SQL\n"})

    restore.sync_down(dbx)
    restore.sync_down(dbx)

    assert dbx.downloads == ["/Shared/Team/dump.sql"]
    assert (local / "dump.sql").read_text() == "SQL\nedited by hook\n"


def test_unmounted_shared_folder_by_namespace_path(local, monkeypatch):
    monkeypatch.setattr(restore, "DROPBOX_FOLDER", "ns:42")
    dbx = FakeDropbox({"ns:42/x.txt": b"X", "ns:42/Sub/y.txt": b"Y"})

    restore.sync_down(dbx)

    assert dbx.listed == ["ns:42", "ns:42/Sub"]
    assert (local / "Sub" / "y.txt").read_bytes() == b"Y"


def test_ignored_and_bookkeeping_files_are_skipped(local):
    local.mkdir()
    (local / "ignore.txt").write_text("*.mp4\n")
    dbx = FakeDropbox({"/Shared/Team/movie.mp4": b"M", "/Shared/Team/checksums.txt": b"C", "/Shared/Team/a.txt": b"A"})

    restore.sync_down(dbx)

    assert dbx.downloads == ["/Shared/Team/a.txt"]
    assert (local / "ignore.txt").read_text() == "*.mp4\n"


def test_failed_download_is_retried_next_pass(local):
    dbx = FakeDropbox(TEAM)
    dbx.fail.add("/Shared/Team/b.txt")

    restore.sync_down(dbx)
    assert not (local / "b.txt").exists()
    assert "b.txt" not in json.loads((local / ".restore-state.json").read_text())

    dbx.downloads.clear()
    restore.sync_down(dbx)
    assert dbx.downloads == ["/Shared/Team/b.txt"]
    assert (local / "b.txt").read_bytes() == b"B"


def test_files_dropbox_cannot_download_are_skipped(local, monkeypatch):
    dbx = FakeDropbox({"/Shared/Team/notes.paper": b"", "/Shared/Team/a.txt": b"A"})
    listing = dbx.files_list_folder

    def list_with_paper(path):
        result = listing(path)
        for entry in result.entries:
            entry.is_downloadable = not entry.name.endswith(".paper")
        return result
    monkeypatch.setattr(dbx, "files_list_folder", list_with_paper)

    restore.sync_down(dbx)
    assert dbx.downloads == ["/Shared/Team/a.txt"]


def test_state_is_saved_when_a_pass_fails_part_way(local):
    dbx = FakeDropbox(TEAM)
    restore.sync_down(dbx)

    def broken(cursor):
        raise ConnectionError("network down")
    dbx.files_list_folder_continue = broken
    dbx.files["/Shared/Team/a.txt"] = b"A2"
    with pytest.raises(ConnectionError):
        restore.sync_down(dbx)

    # The file downloaded before the failure is remembered.
    assert json.loads((local / ".restore-state.json").read_text())["a.txt"] == dropbox_hash(b"A2")
