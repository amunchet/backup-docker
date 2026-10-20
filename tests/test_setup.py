import base64
import json
import os
import stat
import subprocess
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

import setup


class FakeDropbox:
    """Stands in for setup.post: the OAuth token endpoint and the few API routes setup uses."""

    def __init__(self):
        self.codes = {"good-code": "refresh-1", "second-code": "refresh-2"}
        # Like Dropbox, calls only reach inside the folder they're rooted at: your own folder by
        # default, or a shared or team folder outside it (here namespace 42) when rooted at that.
        self.folders = {None: {"/Backups/server1", "/Shared/Team"}, "42": {"", "/Sub"}}
        self.authorized = []

    def __call__(self, url, data, headers):
        if url.endswith("/oauth2/token"):
            if base64.b64decode(headers["Authorization"].split()[1]).endswith(b":wrong"):
                raise setup.DropboxError("invalid_client: Invalid client_id or client_secret")
            form = dict(urllib.parse.parse_qsl(data.decode()))
            if form["grant_type"] == "authorization_code":
                if form["code"] not in self.codes:
                    raise setup.DropboxError("code doesn't exist or has expired")
                return {"refresh_token": self.codes[form["code"]]}
            if form["refresh_token"] not in self.codes.values():
                raise setup.DropboxError("invalid_grant")
            return {"access_token": "token"}
        route, body = url.split("/2/", 1)[1], json.loads(data)
        if route == "users/get_current_account":
            return {"email": "me@example.com", "name": {"display_name": "Me"}, "root_info": {"root_namespace_id": "7"}}
        if route == "files/list_folder":
            root = json.loads(headers.get("Dropbox-API-Path-Root", "{}"))
            if body["path"] in self.folders.get(root.get("namespace_id"), ()):
                return {"entries": []}
            raise setup.DropboxError("path/not_found/")
        if route == "sharing/list_folders":
            return {"entries": [{"name": "Team", "shared_folder_id": "1", "path_display": "/Shared/Team",
                                 "access_type": {".tag": "editor"}}], "cursor": "more"}
        if route == "sharing/list_folders/continue":
            return {"entries": [{"name": "Clients", "shared_folder_id": "42", "access_type": {".tag": "viewer"}}]}
        if route == "sharing/list_mountable_folders":
            return {"entries": [{"name": "Clients", "shared_folder_id": "42", "access_type": {".tag": "viewer"}},
                                {"name": "Engineering", "shared_folder_id": "43", "access_type": {".tag": "editor"},
                                 "is_team_folder": True}]}
        raise AssertionError(f"unexpected route {route}")


@pytest.fixture
def dropbox(tmp_path, monkeypatch):
    """A fake Dropbox, and backup-docker and dropbox-mcp folders in tmp_path."""
    for part, marker in (("backup-docker", "main.py"), ("dropbox-mcp", "server.py")):
        (tmp_path / part).mkdir()
        (tmp_path / part / marker).write_text("")
    monkeypatch.setattr(setup, "BACKUP_DIR", str(tmp_path / "backup-docker"))
    monkeypatch.setattr(setup, "MCP_DIR", str(tmp_path / "dropbox-mcp"))
    fake = FakeDropbox()
    monkeypatch.setattr(setup, "post", fake)
    return fake


def answer(monkeypatch, *answers):
    """Feed these answers to input(), failing if setup asks for more."""
    remaining = list(answers)

    def fake_input(prompt):
        assert remaining, f"setup asked more than expected: {prompt}"
        return remaining.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)
    return remaining


def test_backup_setup_links_dropbox_and_writes_env(dropbox, tmp_path, monkeypatch, capsys):
    target = tmp_path / "restored"
    left = answer(monkeypatch, "key", "secret", "wrong-code", "good-code", "restore", str(target),
                  "Backups/server1/", "600")

    setup.setup_backup(setup.BACKUP_DIR)

    assert left == []
    env_file = tmp_path / "backup-docker" / ".env"
    assert setup.read_env(env_file) == {
        "APP_KEY": "key", "APP_SECRET": "secret", "DROPBOX_REFRESH_TOKEN": "refresh-1", "SYNC_DIRECTION": "RESTORE",
        "WATCH_FOLDER": str(target), "DROPBOX_FOLDER": "/Backups/server1", "SLEEP_TIME": "600",
    }
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert target.is_dir()
    out = capsys.readouterr().out
    assert urllib.parse.urlencode({"scope": setup.BACKUP_SCOPES}) in out
    assert "didn't accept that" in out
    assert "ns:42" in out and "/Shared/Team" in out  # shared folders listed for a restore
    assert "Clients (viewer, not added to your Dropbox)" in out and "Engineering (editor, team folder)" in out


def test_restore_setup_reaches_a_folder_outside_yours_by_its_ns_path(dropbox, tmp_path, monkeypatch, capsys):
    """A team folder, or a shared folder not added to your Dropbox, as ./setup.py shared lists it."""
    left = answer(monkeypatch, "key", "secret", "good-code", "restore", str(tmp_path / "restored"),
                  "/Sub", "ns:42/Missing", "ns:42/Sub/", "600")
    setup.setup_backup(setup.BACKUP_DIR)
    assert left == []
    assert setup.read_env(tmp_path / "backup-docker" / ".env")["DROPBOX_FOLDER"] == "ns:42/Sub"
    out = capsys.readouterr().out
    assert out.count("That doesn't work: path/not_found/") == 2 and "Found it in Dropbox." in out
    assert setup.folder_problem("token", "ns:42") is None


def test_backup_setup_keeps_a_working_link_and_other_settings(dropbox, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    env_file = tmp_path / "backup-docker" / ".env"
    env_file.write_text(f"APP_KEY=key\nAPP_SECRET=secret\nDROPBOX_REFRESH_TOKEN=refresh-1\nDEBUG_LEVEL=DEBUG\n"
                        f"WATCH_FOLDER={tmp_path / 'data'}\nDROPBOX_FOLDER=/Backups/new\n")
    left = answer(monkeypatch, "", "", "", "", "", "", "")  # keep everything

    setup.setup_backup(setup.BACKUP_DIR)

    assert left == []
    env = setup.read_env(env_file)
    assert env["DROPBOX_REFRESH_TOKEN"] == "refresh-1"
    assert env["DEBUG_LEVEL"] == "DEBUG"
    assert (env["SYNC_DIRECTION"], env["DROPBOX_FOLDER"], env["SLEEP_TIME"]) == ("BACKUP", "/Backups/new", "300")


def test_backup_setup_refuses_a_missing_folder_to_back_up(dropbox, tmp_path, monkeypatch, capsys):
    left = answer(monkeypatch, "key", "secret", "good-code", "backup", str(tmp_path / "nope"), str(tmp_path),
                  "/Backups/server1", "60")
    setup.setup_backup(setup.BACKUP_DIR)
    assert left == []
    assert "nope isn't a folder" in capsys.readouterr().out


def test_mcp_setup_relinks_with_write_permission_when_folders_become_writable(dropbox, tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "dropbox-mcp" / ".env"
    env_file.write_text("APP_KEY=key\nAPP_SECRET=secret\nDROPBOX_REFRESH_TOKEN=refresh-1\nWRITABLE_PATHS=\n")
    left = answer(monkeypatch, "", "", "/Shared/Team, ns:42", "", "n", "second-code", "files.example.com")

    setup.setup_mcp()

    assert left == []
    env = setup.read_env(env_file)
    assert env["DROPBOX_REFRESH_TOKEN"] == "refresh-2"
    assert (env["WRITABLE_PATHS"], env["WRITE_PERMISSION"], env["DROPBOX_TEAM_SPACE"], env["MCP_HOST"]) == (
        "/Shared/Team,ns:42", "dropbox:write", "false", "files.example.com")
    out = capsys.readouterr().out
    assert urllib.parse.urlencode({"scope": setup.MCP_SCOPES + " files.content.write"}) in out
    assert "files.example.com {" in out and "reverse_proxy dropbox-mcp:8000" in out
    assert "-p mcp:dropbox,dropbox:write" in out


def test_mcp_setup_read_only_never_asks_for_write_access(dropbox, tmp_path, monkeypatch, capsys):
    left = answer(monkeypatch, "key", "secret", "", "", "good-code", "")
    setup.setup_mcp()
    assert left == []
    env = setup.read_env(tmp_path / "dropbox-mcp" / ".env")
    assert (env["WRITABLE_PATHS"], env["WRITE_PERMISSION"]) == ("", "dropbox:write")
    assert urllib.parse.urlencode({"scope": setup.MCP_SCOPES}) + "\n" in capsys.readouterr().out  # no write scope


def test_wrong_app_secret_stops_with_a_hint(dropbox, monkeypatch):
    answer(monkeypatch, "key", "wrong", "good-code")
    monkeypatch.setattr(sys, "argv", ["setup.py", "backup"])
    with pytest.raises(SystemExit) as exit:
        setup.main()
    assert "Check the app key and secret" in str(exit.value.code)


def test_check_reports_each_part(dropbox, tmp_path, capsys):
    (tmp_path / "backup-docker" / ".env").write_text(
        f"APP_KEY=k\nAPP_SECRET=s\nDROPBOX_REFRESH_TOKEN=refresh-1\nSYNC_DIRECTION=RESTORE\n"
        f"WATCH_FOLDER={tmp_path}\nDROPBOX_FOLDER=/Missing\n")
    (tmp_path / "dropbox-mcp" / ".env").write_text("APP_KEY=k\nAPP_SECRET=s\nDROPBOX_REFRESH_TOKEN=revoked\n")

    assert setup.check(setup.BACKUP_DIR) is False  # a restore needs its Dropbox folder to exist
    assert setup.check(setup.MCP_DIR) is False
    out = capsys.readouterr().out
    assert "Dropbox folder /Missing: path/not_found/" in out
    assert "Dropbox link: FAILED" in out


class FakeSystem:
    """Stands in for subprocess.run: records each command, writes and removes files like tee and rm,
    and answers for systemd, journald and Docker."""

    def __init__(self):
        self.commands = []
        self.state = "active"
        self.log = "Started Dropbox backup/restore.\n[DROPBOX-BACKUP INFO] BACKUP: /data -> Dropbox /Backups/server1\n"
        self.docker = ""

    def __call__(self, command, input=None, **kwargs):
        self.commands.append(command)
        program, *args = command[1:] if command[0] == "sudo" else command
        stdout = ""
        if program == "tee":
            with open(args[0], "w") as f:
                f.write(input)
        elif program == "rm":
            os.remove(args[0])
        elif program == "systemctl" and args[0] == "is-active":
            stdout = self.state + "\n"
        elif program == "journalctl":
            stdout = self.log
        elif program == "docker":
            stdout = self.docker
        return subprocess.CompletedProcess(command, 0, stdout, "")


@pytest.fixture
def system(dropbox, tmp_path, monkeypatch):
    """A machine that systemd runs, where setup runs as alice, who can sudo."""
    for folder in ("run-systemd", "units", "data"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(setup, "SYSTEMD", str(tmp_path / "run-systemd"))
    monkeypatch.setattr(setup, "UNIT_DIR", str(tmp_path / "units"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(setup.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="alice"))
    monkeypatch.setattr(setup.shutil, "which", lambda program: f"/usr/bin/{program}")
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("SUDO_USER", raising=False)
    fake = FakeSystem()
    monkeypatch.setattr(setup.subprocess, "run", fake)
    return fake


def working_env(folder, data):
    (folder / ".env").write_text(f"APP_KEY=key\nAPP_SECRET=secret\nDROPBOX_REFRESH_TOKEN=refresh-1\n"
                                 f"SYNC_DIRECTION=BACKUP\nWATCH_FOLDER={data}\nDROPBOX_FOLDER=/Backups/server1\n")


def test_service_install_sets_up_installs_and_starts_it(system, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "backup-docker"
    python = str(folder / "venv" / "bin" / "python")
    left = answer(monkeypatch, "key", "secret", "good-code", "backup", str(tmp_path / "data"), "/Backups/server1", "300")

    setup.setup_service(setup.BACKUP_DIR)

    assert left == []
    unit = (tmp_path / "units" / "dropbox-backup.service").read_text()
    assert "User=alice\n" in unit and f"WorkingDirectory={folder}\n" in unit
    assert f'ExecStart="{python}" "{folder / "main.py"}"\n' in unit
    assert [sys.executable, "-m", "venv", "--clear", str(folder / "venv")] in system.commands
    assert [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
            str(folder / "requirements.txt")] in system.commands
    assert system.commands[-5:-2] == [["sudo", "systemctl", "daemon-reload"],
                                      ["sudo", "systemctl", "enable", "--quiet", "dropbox-backup"],
                                      ["sudo", "systemctl", "start", "dropbox-backup"]]
    assert setup.read_env(folder / ".env")["WATCH_FOLDER"] == str(tmp_path / "data")
    out = capsys.readouterr().out
    assert "  | [DROPBOX-BACKUP INFO] BACKUP" in out and "dropbox-backup is running" in out
    assert "sudo journalctl -u dropbox-backup -f" in out


def test_service_keeps_working_settings_and_is_named_after_its_copy(system, tmp_path, monkeypatch, capsys):
    copy = tmp_path / "backup-docker-photos"
    (copy / "venv" / "bin").mkdir(parents=True)
    (copy / "venv" / "bin" / "python").write_text("")
    (copy / "main.py").write_text("")
    working_env(copy, tmp_path / "data")
    unit = tmp_path / "units" / "dropbox-backup-photos.service"
    unit.write_text(f"WorkingDirectory={copy}\nExecStart=old\n")  # installed before
    left = answer(monkeypatch, "y")  # keep the settings; Dropbox isn't linked again

    setup.setup_service(str(copy))

    assert left == []
    assert f"WorkingDirectory={copy}\n" in unit.read_text() and "ExecStart=old" not in unit.read_text()
    assert [sys.executable, "-m", "venv", str(copy / "venv")] in system.commands  # a working venv is kept
    stop = system.commands.index(["sudo", "systemctl", "stop", "dropbox-backup-photos"])
    assert system.commands[stop + 1] == ["sudo", "tee", str(unit)]  # the old one stops before it's replaced
    assert f"./setup.py remove-service {os.path.relpath(copy)}" in capsys.readouterr().out


def test_service_that_does_not_start_shows_why(system, tmp_path, monkeypatch, capsys):
    working_env(tmp_path / "backup-docker", tmp_path / "data")
    answer(monkeypatch, "y")
    system.state, system.log = "activating", "Dropbox refused the saved settings: AuthError\n"
    with pytest.raises(SystemExit) as exit:
        setup.setup_service(setup.BACKUP_DIR)
    assert "didn't start" in exit.value.code
    assert "  | Dropbox refused the saved settings" in capsys.readouterr().out


@pytest.mark.parametrize("situation, message", [
    ("no systemd", "systemd isn't running"),
    ("running in Docker", "also running in Docker"),
    ("run with sudo", "without sudo"),
    ("another folder's service", "Nothing was changed"),
])
def test_service_refuses_before_changing_anything(system, tmp_path, monkeypatch, situation, message):
    if situation == "no systemd":
        os.rmdir(setup.SYSTEMD)
    elif situation == "running in Docker":
        system.docker = "0123abcd\n"
    elif situation == "run with sudo":
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_USER", "alice")
        answer(monkeypatch, "n")  # don't run it as root
    else:
        (tmp_path / "units" / "dropbox-backup.service").write_text("WorkingDirectory=/opt/backup-docker\n")
        answer(monkeypatch, "n")  # don't switch it over
    with pytest.raises(SystemExit) as exit:
        setup.setup_service(setup.BACKUP_DIR)
    assert message in exit.value.code
    assert not any("tee" in command or "systemctl" in command for command in system.commands)


def test_remove_service_stops_it_and_deletes_it(system, tmp_path, monkeypatch, capsys):
    unit = tmp_path / "units" / "dropbox-backup.service"
    unit.write_text(f"WorkingDirectory={tmp_path / 'backup-docker'}\n")
    answer(monkeypatch, "y")

    setup.setup_service(setup.BACKUP_DIR, remove=True)

    assert not unit.exists()
    assert system.commands == [["sudo", "systemctl", "disable", "--now", "--quiet", "dropbox-backup"],
                               ["sudo", "rm", str(unit)], ["sudo", "systemctl", "daemon-reload"]]
    with pytest.raises(SystemExit) as exit:
        setup.setup_service(setup.BACKUP_DIR, remove=True)
    assert "no dropbox-backup service" in exit.value.code
