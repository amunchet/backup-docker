import fcntl
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from dropbox.common import PathRoot
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


@pytest.mark.parametrize("folder, namespace, path", [
    ("/Backups/", None, "/Backups"),
    ("/", None, ""),
    # A team folder, or a shared folder not added to your Dropbox: Dropbox only reaches
    # it with calls rooted at it, so it isn't found as a path from your own folder.
    ("ns:42", "42", ""),
    ("ns:42/Sub/", "42", "/Sub"),
])
def test_passes_reach_the_folder_from_the_right_root(settings, monkeypatch, folder, namespace, path):
    dbx = MagicMock()
    monkeypatch.setattr(main, "dropbox_client", lambda: dbx)
    monkeypatch.setattr(main, "DROPBOX_FOLDER", folder)
    main.main()
    assert dbx.with_path_root.call_args_list == ([call(PathRoot.namespace_id(namespace))] if namespace else [])
    settings["backup"].assert_called_once_with(dbx.with_path_root.return_value if namespace else dbx, path)


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


def test_a_second_copy_refuses_to_start():
    """While one copy runs (e.g. the service), another (e.g. from cron) stops before doing anything."""
    with open(Path(main.__file__).with_name("main.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        second = subprocess.run([sys.executable, main.__file__, "--once"], capture_output=True, text=True, timeout=60)
    assert second.returncode == 1 and "already running" in second.stderr
