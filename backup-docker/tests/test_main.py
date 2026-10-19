import sys
from unittest.mock import MagicMock

import pytest
from dropbox.exceptions import BadInputError

import backup
import main
import restore


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Valid settings, a fake Dropbox client, and recorders in place of the real passes."""
    monkeypatch.setattr(main, "WATCH_FOLDER", str(tmp_path))
    monkeypatch.setattr(main, "DROPBOX_FOLDER", "/Backups")
    monkeypatch.setattr(main, "SYNC_DIRECTION", "BACKUP")
    monkeypatch.setattr(main, "dropbox_client", MagicMock)
    monkeypatch.setattr(sys, "argv", ["main.py", "--once"])
    passes = {"backup": MagicMock(), "restore": MagicMock()}
    monkeypatch.setattr(backup, "monitor_and_upload", passes["backup"])
    monkeypatch.setattr(restore, "sync_down", passes["restore"])
    return passes


@pytest.mark.parametrize("direction, ran", [("BACKUP", "backup"), ("RESTORE", "restore")])
def test_once_runs_one_pass_in_the_chosen_direction(settings, monkeypatch, direction, ran):
    monkeypatch.setattr(main, "SYNC_DIRECTION", direction)
    main.main()
    assert settings[ran].call_count == 1
    assert sum(p.call_count for p in settings.values()) == 1


@pytest.mark.parametrize("name, value", [
    ("SYNC_DIRECTION", "SIDEWAYS"),  # a typo must not quietly pick restore
    ("DROPBOX_FOLDER", None),
    ("WATCH_FOLDER", "/no/such/folder"),
])
def test_bad_settings_stop_before_syncing(settings, monkeypatch, name, value):
    monkeypatch.setattr(main, name, value)
    with pytest.raises(SystemExit):
        main.main()
    assert not any(p.called for p in settings.values())


def test_refused_credentials_stop_with_a_hint(settings, monkeypatch):
    dbx = MagicMock()
    dbx.users_get_current_account.side_effect = BadInputError("req", '{"error": "invalid_client"}')
    monkeypatch.setattr(main, "dropbox_client", lambda: dbx)
    with pytest.raises(SystemExit) as exit:
        main.main()
    assert "invalid_client" in exit.value.code and "setup.py check" in exit.value.code
    assert not any(p.called for p in settings.values())


def test_a_failed_pass_does_not_stop_the_loop(settings, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py"])
    settings["backup"].side_effect = [ConnectionError("network down"), None, None]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(main.time, "sleep", sleep)

    with pytest.raises(KeyboardInterrupt):
        main.main()
    assert settings["backup"].call_count == 3
