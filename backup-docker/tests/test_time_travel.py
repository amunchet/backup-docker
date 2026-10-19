import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dropbox.files import FileMetadata

import time_travel


def revision(day, size, rev):
    return FileMetadata(name="report.docx", id="id:r", path_display="/Work/report.docx", size=size, rev=rev,
                        client_modified=datetime(2025, 9, day), server_modified=datetime(2025, 9, day, 12))


# Newest first, as Dropbox lists them.
REVISIONS = [revision(day, size, f"{day:09x}") for day, size in [(9, 300), (8, 200), (7, 100), (6, 200), (5, 100), (4, 200)]]


def fake_dropbox():
    """A client whose list_revisions pages through REVISIONS with before_rev."""
    dbx = MagicMock()

    def list_revisions(path, limit, before_rev):
        start = 0 if before_rev is None else [r.rev for r in REVISIONS].index(before_rev) + 1
        page = REVISIONS[start:start + limit]
        return SimpleNamespace(entries=page, has_more=start + limit < len(REVISIONS))
    dbx.files_list_revisions.side_effect = list_revisions
    dbx.files_restore.return_value = revision(10, 200, "00000000a")
    return dbx


def test_parse_date():
    assert time_travel.parse_date("2025-09-01") == datetime(2025, 9, 1, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert time_travel.parse_date("2025-09-01T10:00:00+02:00") == datetime(2025, 9, 1, 8, tzinfo=timezone.utc)


def test_find_revision_pages_until_the_nth_match():
    on = time_travel.parse_date("2025-09-07")
    chosen, matches, pages, _ = time_travel.find_revision(fake_dropbox(), "/Work/report.docx", on, size=200, nth=2,
                                                          per_page=2)
    assert chosen.rev == f"{4:09x}"  # 200-byte versions on/before Sep 7: Sep 6, then Sep 4
    assert [m.rev for m in matches] == [f"{6:09x}", f"{4:09x}"]
    assert pages == 3


def test_find_revision_without_a_match_reports_the_oldest():
    on = time_travel.parse_date("2025-09-01")
    chosen, matches, pages, oldest = time_travel.find_revision(fake_dropbox(), "/x", on, per_page=4)
    assert (chosen, matches, pages, oldest.rev) == (None, [], 2, f"{4:09x}")


def run(monkeypatch, capsys, *args):
    dbx = fake_dropbox()
    monkeypatch.setattr(time_travel, "dropbox_client", lambda: dbx)
    monkeypatch.setattr(sys, "argv", ["time_travel.py", "/Work/report.docx", *args])
    time_travel.main()
    return dbx, capsys.readouterr().out


def test_dry_run_does_not_restore(monkeypatch, capsys):
    dbx, out = run(monkeypatch, capsys, "--date", "2025-09-07")
    assert f"rev:               {7:09x}" in out and "Dry run only" in out
    dbx.files_restore.assert_not_called()


def test_execute_restores_the_chosen_revision(monkeypatch, capsys):
    dbx, out = run(monkeypatch, capsys, "--date", "2025-09-07", "--size", "100", "--nth", "2", "--execute")
    dbx.files_restore.assert_called_once_with("/Work/report.docx", f"{5:09x}")
    assert "Restored successfully." in out


def test_list_shows_the_requested_number_of_matches(monkeypatch, capsys):
    dbx, out = run(monkeypatch, capsys, "--date", "2025-09-08", "--list", "3")
    assert "[showing 3]" in out
    dbx.files_restore.assert_not_called()


def test_no_match_exits_with_1(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit:
        run(monkeypatch, capsys, "--date", "2025-08-01", "--execute")
    assert exit.value.code == 1
