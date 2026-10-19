import base64
import json
import os
import stat
import sys
import urllib.parse

import pytest

import setup


class FakeDropbox:
    """Stands in for setup.post: the OAuth token endpoint and the few API routes setup uses."""

    def __init__(self):
        self.codes = {"good-code": "refresh-1", "second-code": "refresh-2"}
        self.folders = {"/Backups/server1", "/Shared/Team", "ns:42"}
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
            if body["path"] in self.folders:
                return {"entries": []}
            raise setup.DropboxError("path/not_found/")
        if route == "sharing/list_folders":
            return {"entries": [{"name": "Team", "shared_folder_id": "1", "path_display": "/Shared/Team",
                                 "access_type": {".tag": "editor"}}], "cursor": "more"}
        if route == "sharing/list_folders/continue":
            return {"entries": [{"name": "Clients", "shared_folder_id": "42", "access_type": {".tag": "viewer"}}]}
        if route == "sharing/list_mountable_folders":
            return {"entries": [{"name": "Clients", "shared_folder_id": "42", "access_type": {".tag": "viewer"}}]}
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
    assert "files.content.write" not in capsys.readouterr().out


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
