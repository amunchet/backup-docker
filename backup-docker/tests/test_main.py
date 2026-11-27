import fcntl
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
import requests
from dropbox.common import PathRoot
from dropbox.exceptions import AuthError, BadInputError, InternalServerError

import backup
import main
import restore

# What a pass sees while the network or DNS is down.
DOWN = requests.exceptions.ConnectionError("Failed to resolve 'api.dropboxapi.com'")


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


@pytest.fixture
def clock(settings, monkeypatch):
    """Running until stopped, with a fake clock that sleeping moves on: a pass every 10s, giving up after 30s."""
    now = [0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] > 1000:  # pytest.fail isn't an Exception, so the loop can't swallow it
            pytest.fail("Still running after 1000s")
    monkeypatch.setattr(main.time, "sleep", sleep)
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(main, "SLEEP_TIME", 10)
    monkeypatch.setattr(main, "GIVE_UP_AFTER", 30)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    return now


def dropbox_answers(monkeypatch, *answers):
    """A fake Dropbox whose account check gives these answers in turn, raising the exceptions."""
    dbx = MagicMock()
    dbx.users_get_current_account.side_effect = answers
    monkeypatch.setattr(main, "dropbox_client", lambda: dbx)


def test_an_outage_at_startup_is_waited_out(settings, monkeypatch, clock):
    """E.g. a reboot, before the network or DNS is up: it keeps trying, instead of exiting into a restart."""
    dropbox_answers(monkeypatch, DOWN, InternalServerError("req", 503, ""), MagicMock(), KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        main.main()
    assert settings["backup"].call_count == 1


def test_failing_past_the_limit_exits_so_it_is_restarted(settings, monkeypatch, clock):
    dropbox_answers(monkeypatch, *[DOWN] * 10)
    with pytest.raises(SystemExit) as exit:
        main.main()
    assert "failed for" in exit.value.code and "Failed to resolve" in exit.value.code
    assert clock[0] == 30 and not settings["backup"].called


def test_outages_with_a_good_pass_between_them_never_add_up(settings, monkeypatch, clock):
    dropbox_answers(monkeypatch, DOWN, DOWN, DOWN, MagicMock(), DOWN, DOWN, DOWN, MagicMock(), KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        main.main()
    assert settings["backup"].call_count == 2


def test_settings_refused_while_running_stop_it_at_once(settings, monkeypatch, clock):
    """E.g. the Dropbox link was removed: waiting can't fix that, and a service that keeps exiting shows it."""
    dropbox_answers(monkeypatch, MagicMock(), AuthError("req", "invalid_access_token"))
    with pytest.raises(SystemExit) as exit:
        main.main()
    assert "setup.py check" in exit.value.code
    assert settings["backup"].call_count == 1


def test_once_reports_an_outage_straight_away(settings, monkeypatch):
    """A single pass, e.g. from cron, fails rather than waiting out the outage."""
    dropbox_answers(monkeypatch, DOWN)
    with pytest.raises(requests.exceptions.ConnectionError):
        main.main()
    assert not settings["backup"].called


def test_an_outage_is_logged_in_one_line_and_a_bug_with_its_traceback(settings, clock, caplog):
    settings["backup"].side_effect = [DOWN, KeyError("bug"), KeyboardInterrupt]
    with pytest.raises(KeyboardInterrupt):
        main.main()
    assert [bool(r.exc_info) for r in caplog.records if r.levelname == "ERROR"] == [False, True]


def test_a_second_copy_refuses_to_start():
    """While one copy runs (e.g. the service), another (e.g. from cron) stops before doing anything."""
    with open(Path(main.__file__).with_name("main.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        second = subprocess.run([sys.executable, main.__file__, "--once"], capture_output=True, text=True, timeout=60)
    assert second.returncode == 1 and "already running" in second.stderr
