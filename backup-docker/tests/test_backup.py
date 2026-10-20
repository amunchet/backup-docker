import hashlib
from unittest.mock import MagicMock

import pytest
from dropbox.files import WriteMode

import backup
from backup import calculate_md5, read_checksums, write_checksums


@pytest.fixture
def watch(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "WATCH_FOLDER", str(tmp_path))
    return tmp_path


def uploaded_paths(dbx):
    return sorted(c.args[1] for c in dbx.files_upload.call_args_list)


def test_calculate_md5(tmp_path):
    (tmp_path / "f").write_bytes(b"test data")
    assert calculate_md5(tmp_path / "f") == hashlib.md5(b"test data").hexdigest()


def test_read_checksums_legacy_format(tmp_path):
    (tmp_path / "checksums.txt").write_text("file1 abcdef1234567890\nfile2 1234567890abcdef\n")
    assert read_checksums(tmp_path / "checksums.txt") == {"file1": "abcdef1234567890", "file2": "1234567890abcdef"}


def test_checksums_round_trip_with_spaces(tmp_path):
    checksums = {"file1": "a" * 32, "dir/file 2.txt": "b" * 32}
    write_checksums(tmp_path / "checksums.txt", checksums)
    assert (tmp_path / "checksums.txt").read_text() == f"{'b' * 32} dir/file 2.txt\n{'a' * 32} file1\n"
    assert read_checksums(tmp_path / "checksums.txt") == checksums


def test_monitor_and_upload_uploads_new_and_nested_files_only(watch):
    (watch / "file1").write_text("unchanged")
    (watch / "file2").write_text("new")
    (watch / "sub" / "dir").mkdir(parents=True)
    (watch / "sub" / "dir" / "file 3.txt").write_text("nested")
    (watch / "scratch.tmp").write_text("ignored")
    (watch / "ignore.txt").write_text("# comment\n*.tmp\n")
    write_checksums(watch / "checksums.txt", {"file1": calculate_md5(watch / "file1")})
    dbx = MagicMock()

    backup.monitor_and_upload(dbx, "/dropbox_folder")

    assert uploaded_paths(dbx) == ["/dropbox_folder/file2", "/dropbox_folder/sub/dir/file 3.txt"]
    assert dbx.files_upload.call_args.kwargs["mode"] == WriteMode.overwrite
    assert set(read_checksums(watch / "checksums.txt")) == {"file1", "file2", "sub/dir/file 3.txt"}

    dbx.reset_mock()
    backup.monitor_and_upload(dbx, "/dropbox_folder")
    dbx.files_upload.assert_not_called()


def test_failed_upload_is_retried_next_pass(watch):
    (watch / "file").write_text("data")
    dbx = MagicMock()
    dbx.files_upload.side_effect = [ConnectionError("network down"), None]

    backup.monitor_and_upload(dbx, "/dropbox_folder")
    assert read_checksums(watch / "checksums.txt") == {}

    backup.monitor_and_upload(dbx, "/dropbox_folder")
    assert dbx.files_upload.call_count == 2
    assert set(read_checksums(watch / "checksums.txt")) == {"file"}


@pytest.mark.parametrize("data, appends", [
    (b"0123456789", [(b"4567", 4)]),  # start, one append, finish with the rest
    (b"01234567", []),                # exactly two chunks: start, then finish
])
def test_large_files_upload_in_chunks(tmp_path, monkeypatch, data, appends):
    monkeypatch.setattr(backup, "CHUNK_SIZE", 4)
    (tmp_path / "big").write_bytes(data)
    dbx = MagicMock()
    dbx.files_upload_session_start.return_value.session_id = "s1"
    calls = []
    dbx.files_upload_session_append_v2.side_effect = lambda chunk, cursor: calls.append((chunk, cursor.offset))
    dbx.files_upload_session_finish.side_effect = lambda chunk, cursor, commit: calls.append(
        (chunk, cursor.offset, commit.path))

    backup.upload(dbx, tmp_path / "big", "/x/big")

    dbx.files_upload.assert_not_called()
    dbx.files_upload_session_start.assert_called_once_with(b"0123")
    last = 4 + 4 * len(appends)
    assert calls == appends + [(data[last:], last, "/x/big")]
