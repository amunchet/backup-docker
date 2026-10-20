#!/usr/bin/env python3
"""
Interactive setup: links your Dropbox app, writes the .env file and checks that
Dropbox can be reached. Needs only Python 3, nothing to install.

    ./setup.py                        asks what to set up
    ./setup.py backup [dir]           back up a folder to Dropbox, or restore one from it, run by Docker
                                      (dir: backup-docker, or a copy of it for another folder)
    ./setup.py service [dir]          the same, run as a systemd service instead: installs it, or
                                      updates it and its settings
    ./setup.py remove-service [dir]   stop and remove that service
    ./setup.py mcp                    the Dropbox MCP server (dropbox-mcp)
    ./setup.py check [part]           test the saved settings (part: backup, mcp or a directory)
    ./setup.py shared [part]          list your shared folders and the path to use for each
"""
import base64
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backup-docker")
MCP_DIR = os.path.join(HERE, "dropbox-mcp")
APP_CONSOLE = "https://www.dropbox.com/developers/apps"
SYSTEMD = "/run/systemd/system"  # exists only on machines that systemd runs
UNIT_DIR = "/etc/systemd/system"

# What each part's Dropbox link may do. The Dropbox app needs all of these ticked on its
# Permissions tab. The MCP server only gets files.content.write when something is writable.
BACKUP_SCOPES = "account_info.read files.metadata.read files.content.read files.content.write sharing.read"
MCP_SCOPES = "account_info.read files.metadata.read files.content.read sharing.read"


class DropboxError(Exception):
    """An error reply from Dropbox, as its short summary (e.g. "path/not_found/...")."""


def read_env(path):
    values = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    return values


def write_env(path, env, keys):
    """Write `keys` first, then anything else already in the file, readable only by you."""
    with open(path, "w") as f:
        for key in keys + [k for k in env if k not in keys]:
            f.write(f"{key}={env.get(key, '')}\n")
    os.chmod(path, 0o600)


def ask(label, default=""):
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def choose(label, options, default):
    """Ask until the answer is one of `options`."""
    while True:
        answer = ask(f"{label} ({'/'.join(options)})", default).lower()
        if answer in options:
            return answer


def post(url, data, headers):
    """POST to Dropbox and return its JSON reply."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=60) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            error = json.loads(body)
            body = error.get("error_summary") or error.get("error_description") or str(error.get("error") or body)
        except ValueError:
            pass
        raise DropboxError(body.strip()) from None


def oauth(env, **form):
    """Call Dropbox's token endpoint as the app."""
    auth = base64.b64encode(f"{env['APP_KEY']}:{env['APP_SECRET']}".encode()).decode()
    return post("https://api.dropboxapi.com/oauth2/token", urllib.parse.urlencode(form).encode(),
                {"Authorization": f"Basic {auth}"})


def api(token, route, body=None, root=None):
    """Call a Dropbox API route, with paths relative to `root` (a Dropbox-API-Path-Root value) if given."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if root:
        headers["Dropbox-API-Path-Root"] = json.dumps(root)
    return post(f"https://api.dropboxapi.com/2/{route}", json.dumps(body).encode(), headers)


def connect(env):
    """An access token from the saved refresh token, and the account it belongs to."""
    token = oauth(env, grant_type="refresh_token", refresh_token=env["DROPBOX_REFRESH_TOKEN"])["access_token"]
    return token, api(token, "users/get_current_account")


def dropbox_path(answer):
    """A Dropbox path as typed, tidied up: '/a/b' or 'ns:<id>/a/b', and '' for the top. Takes ns://<id> too."""
    answer = re.sub(r"^ns:/*", "ns:", answer.strip())
    return (answer if answer.startswith(("/", "ns:")) else "/" + answer).rstrip("/")


def in_folder(folder, path):
    """The Dropbox path of `path`, a path inside the MCP server's DROPBOX_FOLDER `folder` ('' for all of Dropbox)."""
    return f"{folder}/{path.strip('/')}".rstrip("/") if folder else path


def folder_problem(token, path, team_root=None):
    """None if `path` is a folder in Dropbox, otherwise Dropbox's complaint (e.g. "path/not_found/")."""
    # Calls only reach inside the folder they're rooted at: yours, or the team's with team_root. A shared
    # or team folder outside it (ns:<id>/...) is reached by rooting the call at that folder instead.
    root = team_root and {".tag": "root", "root": team_root}
    if path.startswith("ns:"):
        namespace, _, path = path[3:].partition("/")
        root, path = {".tag": "namespace_id", "namespace_id": namespace}, "/" + path
    try:
        api(token, "files/list_folder", {"path": "" if path == "/" else path, "limit": 1}, root)
    except DropboxError as e:
        return str(e)


def ask_app(env):
    """Ask for the Dropbox app's key and secret, offering the ones the other part uses."""
    others = {**read_env(os.path.join(BACKUP_DIR, ".env")), **read_env(os.path.join(MCP_DIR, ".env"))}
    if not (env.get("APP_KEY") or others.get("APP_KEY")):
        print(f"First you need a Dropbox app (README, step 1). At {APP_CONSOLE}")
        print("click Create app, and pick Scoped access and Full Dropbox. On the app's Permissions tab,")
        print(f"tick {', '.join(BACKUP_SCOPES.split())}\nand click Submit.\n")
    print(f"Your Dropbox app's key and secret are on its Settings tab, at {APP_CONSOLE}")
    env["APP_KEY"] = ask("Dropbox app key", env.get("APP_KEY") or others.get("APP_KEY", ""))
    env["APP_SECRET"] = ask("Dropbox app secret", env.get("APP_SECRET") or others.get("APP_SECRET", ""))


def link_dropbox(env, scopes, relink=False):
    """Link Dropbox to the app, unless the saved link still works. Returns (token, account)."""
    if env.get("DROPBOX_REFRESH_TOKEN") and not relink:
        try:
            token, account = connect(env)
            if choose(f"\nDropbox is linked to {account['email']}. Link again?", ["y", "n"], "n") == "n":
                return token, account
        except DropboxError as e:
            if "invalid_client" in str(e):
                raise
            print("\nThe saved Dropbox link doesn't work any more, so it needs linking again.")

    url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": env["APP_KEY"], "response_type": "code", "token_access_type": "offline", "scope": scopes,
    })
    print("\nLink Dropbox:")
    print(f"  1. Open this address in a browser, signed in to the Dropbox account to use:\n\n     {url}\n")
    print("  2. Click Continue, then Allow. Dropbox then shows an access code.")
    while True:
        code = input("  3. Paste the access code here: ").strip()
        try:
            env["DROPBOX_REFRESH_TOKEN"] = oauth(env, grant_type="authorization_code", code=code)["refresh_token"]
            break
        except DropboxError as e:
            if "invalid_client" in str(e):
                raise
            print(f"     Dropbox didn't accept that ({e}). Try again, or press Ctrl+C to stop.")
    token, account = connect(env)
    print(f"Linked to {account['name']['display_name']} <{account['email']}>.")
    return token, account


def print_shared(token):
    """List every shared folder the account can reach, with the path to use for it."""
    folders = {}
    for route in ("sharing/list_folders", "sharing/list_mountable_folders"):
        reply = api(token, route, {"limit": 1000})
        while True:
            for folder in reply["entries"]:
                folders[folder["shared_folder_id"]] = folder
            if not reply.get("cursor"):
                break
            reply = api(token, route + "/continue", {"cursor": reply["cursor"]})
    if not folders:
        print("No shared folders.")
        return
    print("Shared folders (use the path on the left):")
    for folder in sorted(folders.values(), key=lambda f: f["name"].lower()):
        path = folder.get("path_display") or f"ns:{folder['shared_folder_id']}"
        note = ""
        if not folder.get("path_display"):
            note = ", team folder" if folder.get("is_team_folder") else ", not added to your Dropbox"
        print(f"  {path:<40} {folder['name']} ({folder['access_type']['.tag']}{note})")


def setup_backup(folder):
    """Set up a backup-docker folder: what to sync, in which direction, and how often."""
    if not os.path.exists(os.path.join(folder, "main.py")):
        sys.exit(f"{folder} isn't backup-docker or a copy of it.")
    env_file = os.path.join(folder, ".env")
    env = read_env(env_file)
    print(f"Backup/restore setup for {os.path.relpath(folder)}. Press Enter to keep the value in brackets.\n")
    ask_app(env)
    token, account = link_dropbox(env, BACKUP_SCOPES)

    print()
    direction = choose("Back up a folder on this machine to Dropbox, or restore (download) a Dropbox folder here?",
                       ["backup", "restore"], env.get("SYNC_DIRECTION", "backup").lower())
    env["SYNC_DIRECTION"] = direction.upper()

    label = "Folder on this machine to back up" if direction == "backup" else "Folder on this machine to download into"
    while True:
        answer = ask(label, env.get("WATCH_FOLDER", ""))
        local = os.path.abspath(os.path.expanduser(answer)) if answer else ""
        # A restore may also go into a folder that doesn't exist yet.
        if local and (os.path.isdir(local) or (direction == "restore" and not os.path.exists(local))):
            break
        print(f"  {local} isn't a folder." if local else "  Give a folder.")
    env["WATCH_FOLDER"] = local
    if direction == "restore":
        os.makedirs(local, exist_ok=True)
        print("  Files in it that also exist in Dropbox will be replaced by the Dropbox version.\n")
        try:
            print_shared(token)
        except DropboxError as e:
            print(f"(Couldn't list shared folders: {e})")
        print()

    example = "/Backups/server1" if direction == "backup" else "/Backups/server1, or ns:12345 from the list above"
    while True:
        remote = ask(f"Dropbox folder (e.g. {example})", env.get("DROPBOX_FOLDER", ""))
        if not remote:
            continue
        remote = dropbox_path(remote) or "/"
        problem = folder_problem(token, remote)
        if not problem:
            print("  Found it in Dropbox.")
            break
        if "not_found" in problem and direction == "backup":
            print("  It isn't in Dropbox yet; the first backup creates it.")
            break
        print(f"  That doesn't work: {problem}")
    env["DROPBOX_FOLDER"] = remote

    while True:
        env["SLEEP_TIME"] = ask("Seconds to wait between passes", env.get("SLEEP_TIME") or "300")
        if env["SLEEP_TIME"].isdigit():
            break

    write_env(env_file, env, ["APP_KEY", "APP_SECRET", "DROPBOX_REFRESH_TOKEN", "SYNC_DIRECTION", "WATCH_FOLDER",
                              "DROPBOX_FOLDER", "SLEEP_TIME"])
    print(f"\nWrote {os.path.relpath(env_file)}.")


def setup_service(folder, remove=False):
    """Install, update or remove the systemd service that runs a backup-docker folder without Docker."""
    if not os.path.exists(os.path.join(folder, "main.py")):
        sys.exit(f"{folder} isn't backup-docker or a copy of it.")
    if not os.path.isdir(SYSTEMD):
        sys.exit("systemd isn't running on this machine, so it can't run the service. Use Docker instead (README, A2).")
    base = os.path.basename(folder)
    # dropbox-backup for backup-docker, dropbox-backup-photos for a copy named backup-docker-photos
    suffix = base.replace("backup-docker", "", 1) if base.startswith("backup-docker") else f"-{base}"
    name = "dropbox-backup" + re.sub(r"[^A-Za-z0-9_.-]", "-", suffix)
    unit = os.path.join(UNIT_DIR, f"{name}.service")
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    if sudo and not shutil.which("sudo"):
        sys.exit("Installing a service needs root. Run this as root, or install sudo.")
    rel = os.path.relpath(folder)
    here = "" if folder == BACKUP_DIR else f" {rel}"  # how the commands it prints name this folder
    installed = []
    if os.path.exists(unit):
        with open(unit) as f:
            installed = re.findall(r"^WorkingDirectory=(.*)$", f.read(), re.M)

    if remove:
        if not os.path.exists(unit):
            sys.exit(f"There's no {name} service to remove.")
        if choose(f"Stop and remove the {name} service? Its settings and files stay.", ["y", "n"], "n") == "y":
            for command in (["systemctl", "disable", "--now", "--quiet", name], ["rm", unit], ["systemctl", "daemon-reload"]):
                subprocess.run(sudo + command, check=True)
            print(f"Removed {name}. To install it again: ./setup.py service{here}")
        return

    user = pwd.getpwuid(os.geteuid()).pw_name
    if os.environ.get("SUDO_USER") and not sudo:
        print(f"You ran this with sudo, so the service would run as root, not as {os.environ['SUDO_USER']}.")
        if choose("Run it as root?", ["y", "n"], "n") == "n":
            sys.exit("Run ./setup.py service without sudo. It asks for your password when it needs it.")
    if sys.version_info < (3, 8):
        sys.exit(f"The service needs Python 3.8 or newer, and this is Python {sys.version.split()[0]}. Install a "
                 f"newer one and run this setup with it, e.g.: python3.11 setup.py service{here}")
    if re.search(r"[%$\\\"'\n]", folder):
        sys.exit(f"systemd can't run a folder whose path has % $ \\ or quotes in it. Move {rel}, e.g. to /opt.")
    if shutil.which("docker") and subprocess.run(["docker", "compose", "ps", "-q"], cwd=folder,
                                                 capture_output=True, text=True).stdout.strip():
        sys.exit(f"{rel} is also running in Docker. Stop that first, so the two don't both sync the folder:\n"
                 f"  cd {rel} && docker compose down")
    if installed and installed[0] != folder:
        if choose(f"{name} already runs {installed[0]}. Switch it to {folder}?", ["y", "n"], "n") == "n":
            sys.exit("Nothing was changed.")

    print(f"This sets up {rel} as the systemd service {name}.")
    print(f"It starts at boot, restarts after a crash, and runs as {user}, with {user}'s access to files.\n")
    print(f"[1/4] Preparing {rel}/venv, a private Python for the service...")
    venv = os.path.join(folder, "venv")
    python = os.path.join(venv, "bin", "python")
    # --clear rebuilds a venv whose Python has gone, e.g. after an OS upgrade.
    subprocess.run([sys.executable, "-m", "venv"] + ([] if os.path.exists(python) else ["--clear"]) + [venv], check=True)

    print("\n[2/4] Settings")
    env_file = os.path.join(folder, ".env")
    if not (read_env(env_file).get("DROPBOX_REFRESH_TOKEN") and check(folder)
            and choose("Keep these settings?", ["y", "n"], "y") == "y"):
        setup_backup(folder)
    local = read_env(env_file)["WATCH_FOLDER"]
    if not os.access(local, os.R_OK | os.W_OK | os.X_OK):
        sys.exit(f"The service runs as {user}, who can't read and write {local}. Give {user} access, e.g.\n"
                 f"  sudo chown -R {user} {local}\nor run this setup as a user who has it.")

    print("\n[3/4] Installing its Python packages (this can take a minute)...")
    subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r",
                    os.path.join(folder, "requirements.txt")], check=True)

    print(f"\n[4/4] Installing and starting {unit}" + (" (sudo may ask for your password)" if sudo else ""))
    if os.path.exists(unit):
        subprocess.run(sudo + ["systemctl", "stop", name], check=True)
    subprocess.run(sudo + ["tee", unit], stdout=subprocess.DEVNULL, check=True, text=True, input=f"""\
# Written by {os.path.join(HERE, "setup.py")} service{here}, which also changes it.
# Settings are in {env_file}; after editing it: sudo systemctl restart {name}
[Unit]
Description=Dropbox backup/restore ({folder})
Wants=network-online.target
After=network-online.target

[Service]
User={user}
WorkingDirectory={folder}
ExecStart="{python}" "{os.path.join(folder, "main.py")}"
Restart=on-failure
RestartSec=60
SyslogIdentifier={name}

[Install]
WantedBy=multi-user.target
""")
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")  # the log below starts here
    for command in (["daemon-reload"], ["enable", "--quiet", name], ["start", name]):
        subprocess.run(sudo + ["systemctl"] + command, check=True)

    # Bad settings stop it within seconds, and its first log line means Dropbox let it in.
    for _ in range(20):
        time.sleep(1)
        state = subprocess.run(sudo + ["systemctl", "is-active", name], capture_output=True, text=True).stdout.strip()
        log = subprocess.run(sudo + ["journalctl", "-q", "-u", name, "--since", started, "-o", "cat", "--no-pager"],
                             capture_output=True, text=True).stdout
        if state != "active" or "[DROPBOX-BACKUP" in log:
            break
    print("".join(f"  | {line}\n" for line in log.splitlines()[-12:]))
    if state != "active":
        sys.exit(f"{name} didn't start, and the log above says why. Fix that, then run ./setup.py service{here} again.")
    as_root = "sudo " if sudo else ""
    print(f"{name} is running. It starts by itself at boot.\n")
    print(f"  See what it's doing:  {as_root}journalctl -u {name} -f")
    print(f"  Is it running?        {as_root}systemctl status {name}")
    print(f"  Apply an edited .env: {as_root}systemctl restart {name}")
    print(f"  Change settings:      ./setup.py service{here}")
    print(f"  Remove the service:   ./setup.py remove-service{here}")


def setup_mcp():
    """Set up dropbox-mcp: what clients can read and write, team folders, and the Dropbox link."""
    env_file = os.path.join(MCP_DIR, ".env")
    env = read_env(env_file)
    print("Dropbox MCP setup. Press Enter to keep the value in brackets.\n")
    ask_app(env)

    print("\nMCP clients can read all of Dropbox, or only one folder, which is then their top level.")
    print("Use paths like /Projects, or ns:<id>/path for team folders and shared folders that aren't")
    print("in your Dropbox (./setup.py shared lists them). Enter / for all of Dropbox.")
    env["DROPBOX_FOLDER"] = dropbox_path(ask("Folder MCP clients can read", env.get("DROPBOX_FOLDER") or "/"))
    where = f"paths inside {env['DROPBOX_FOLDER']}, like /Outbox" if env["DROPBOX_FOLDER"] else "the same kind of paths"
    print(f"\nThey can write only inside the folders you list here. Use {where}. Enter - for none.")
    was_writable = bool(env.get("WRITABLE_PATHS"))
    answer = ask("Writable folders, comma-separated", env.get("WRITABLE_PATHS", "-"))
    env["WRITABLE_PATHS"] = "" if answer == "-" else ",".join(p.strip() for p in answer.split(",") if p.strip())
    if env["WRITABLE_PATHS"]:
        env["WRITE_PERMISSION"] = ask("Auth0 permission a user needs to write", env.get("WRITE_PERMISSION") or "dropbox:write")
    else:
        env["WRITE_PERMISSION"] = env.get("WRITE_PERMISSION") or "dropbox:write"
    team = "n"  # with one folder, the top level is that folder, and team folders are ns:<id> paths
    if not env["DROPBOX_FOLDER"]:
        team = choose("\nIs this a Dropbox Business account, and should team folders show at the top level?",
                      ["y", "n"], "y" if env.get("DROPBOX_TEAM_SPACE") == "true" else "n")
    env["DROPBOX_TEAM_SPACE"] = "true" if team == "y" else "false"

    scopes = MCP_SCOPES + (" files.content.write" if env["WRITABLE_PATHS"] else "")
    relink = bool(env.get("DROPBOX_REFRESH_TOKEN")) and was_writable != bool(env["WRITABLE_PATHS"])
    if relink:
        print("\nThe writable folders changed, so Dropbox needs linking again with matching permissions.")
    token, account = link_dropbox(env, scopes, relink)

    team_root = account["root_info"]["root_namespace_id"] if team == "y" else None
    while env["DROPBOX_FOLDER"]:
        problem = folder_problem(token, env["DROPBOX_FOLDER"])
        if not problem:
            print(f"  Found {env['DROPBOX_FOLDER']} in Dropbox.")
            break
        print(f"  {env['DROPBOX_FOLDER']} doesn't work: {problem}")
        env["DROPBOX_FOLDER"] = dropbox_path(ask("Folder MCP clients can read (/ for all of Dropbox)",
                                                 env["DROPBOX_FOLDER"]))
    for path in filter(None, env["WRITABLE_PATHS"].split(",")):
        problem = folder_problem(token, in_folder(env["DROPBOX_FOLDER"], path), team_root)
        if problem and "not_folder" not in problem:
            note = " (writing a file there creates it)" if "not_found" in problem else ""
            print(f"  Writable folder {path}: {problem}{note}")

    env["MCP_HOST"] = ask("\nPublic hostname for the MCP server, e.g. dropbox-mcp.example.com", env.get("MCP_HOST", ""))
    write_env(env_file, env, ["APP_KEY", "APP_SECRET", "DROPBOX_REFRESH_TOKEN", "DROPBOX_FOLDER", "WRITABLE_PATHS",
                              "WRITE_PERMISSION", "DROPBOX_TEAM_SPACE", "MCP_HOST"])
    host = env["MCP_HOST"] or "dropbox-mcp.example.com"
    write_note = f" and {env['WRITE_PERMISSION']} (to write)" if env["WRITABLE_PATHS"] else ""
    print(f"\nWrote {os.path.relpath(env_file)}. Next:\n")
    print(f"1. Point DNS for {host} at this server, add this to caddy-auth0's Caddyfile, and reload Caddy")
    print("   (in the caddy-auth0 folder: docker compose exec -w /etc/caddy caddy caddy reload):\n")
    print(f"   {host} {{\n   \tauth0 {{\n   \t\tmcp\n   \t\trequire mcp:dropbox\n   \t}}\n"
          f"   \treverse_proxy dropbox-mcp:8000\n   }}\n")
    print(f"2. Start the server, from {os.path.relpath(MCP_DIR)}:  docker compose up -d --build\n")
    print(f"3. In Auth0, give users the permissions mcp:dropbox (to use it){write_note},")
    print("   or mint a token for a client, in the caddy-auth0 folder:")
    extra = f" -p mcp:dropbox,{env['WRITE_PERMISSION']}" if env["WRITABLE_PATHS"] else ""
    print(f"   ./mcp-token {host} <client name>{extra}\n")
    print(f"4. Point MCP clients at https://{host}/mcp")


def check(folder):
    """Test a part's saved settings. Returns True if everything works."""
    env = read_env(os.path.join(folder, ".env"))
    is_mcp = os.path.exists(os.path.join(folder, "server.py"))
    print(f"{os.path.relpath(folder)}:")
    if not env.get("DROPBOX_REFRESH_TOKEN"):
        print("  Not set up yet.")
        return False
    try:
        token, account = connect(env)
    except DropboxError as e:
        print(f"  Dropbox link: FAILED ({e})")
        return False
    print(f"  Dropbox link: OK ({account['email']})")
    ok = True
    if is_mcp:
        team_root = account["root_info"]["root_namespace_id"] if env.get("DROPBOX_TEAM_SPACE") == "true" else None
        readable = env.get("DROPBOX_FOLDER", "").rstrip("/")  # as the server reads it
        if readable:
            problem = folder_problem(token, readable, team_root)
            print(f"  Folder clients can read {readable}: {problem or 'OK'}")
            ok = not problem
        for path in filter(None, env.get("WRITABLE_PATHS", "").split(",")):
            problem = folder_problem(token, in_folder(readable, path), team_root)
            print(f"  Writable {path}: {'OK' if not problem or 'not_folder' in problem else problem}")
        if not env.get("WRITABLE_PATHS"):
            print("  Writable folders: none, everything is read-only")
    else:
        local, remote = env.get("WATCH_FOLDER", ""), env.get("DROPBOX_FOLDER", "")
        local_ok = os.path.isdir(local)
        print(f"  {env.get('SYNC_DIRECTION', 'BACKUP')} every {env.get('SLEEP_TIME', '60')}s")
        print(f"  Folder here {local or '(not set)'}: {'OK' if local_ok else 'MISSING'}")
        problem = folder_problem(token, remote) if remote else "not set"
        missing_ok = problem and "not_found" in problem and env.get("SYNC_DIRECTION", "BACKUP") == "BACKUP"
        print(f"  Dropbox folder {remote}: {'created on first backup' if missing_ok else problem or 'OK'}")
        ok = local_ok and (not problem or missing_ok)
    return ok


def part_dirs(arg):
    """The folders a check or shared command is about."""
    if arg == "backup":
        return [BACKUP_DIR]
    if arg == "mcp":
        return [MCP_DIR]
    if arg:
        return [os.path.abspath(arg)]
    return [d for d in (BACKUP_DIR, MCP_DIR) if os.path.exists(os.path.join(d, ".env"))]


def main():
    sys.stdout.reconfigure(line_buffering=True)  # keep prints in order with the output of commands it runs
    args = sys.argv[1:]
    target = args[1] if len(args) > 1 else None
    folder = os.path.abspath(target) if target else BACKUP_DIR
    try:
        if not args:
            print("What to set up:\n"
                  "  backup   back up a folder to Dropbox, or restore one from it, run by Docker\n"
                  "  service  the same, run as a systemd service instead of Docker\n"
                  "  mcp      the Dropbox MCP server, run by Docker behind caddy-auth0\n")
        command = args[0] if args else choose("Which one", ["backup", "service", "mcp"], "backup")
        if command == "backup":
            setup_backup(folder)
            print(f"\nStart it (or apply new settings), from {os.path.relpath(folder)}:\n  docker compose up -d --build\n")
            print("See what it's doing:\n  docker compose logs -f\n")
            print(f"Or run it as a systemd service instead of Docker:\n  ./setup.py service{'' if folder == BACKUP_DIR else ' ' + target}")
        elif command in ("service", "remove-service"):
            setup_service(folder, remove=command == "remove-service")
        elif command == "mcp":
            setup_mcp()
        elif command == "check":
            dirs = part_dirs(target)
            if not dirs:
                sys.exit("Nothing is set up yet. Run ./setup.py backup, service or mcp.")
            if not all([check(d) for d in dirs]):
                sys.exit("To fix it, run that part's setup again: ./setup.py backup, service or mcp.")
        elif command == "shared":
            dirs = [d for d in part_dirs(target) if read_env(os.path.join(d, ".env")).get("DROPBOX_REFRESH_TOKEN")]
            if not dirs:
                sys.exit("Link Dropbox first: ./setup.py backup or ./setup.py mcp.")
            print_shared(connect(read_env(os.path.join(dirs[0], ".env")))[0])
        else:
            sys.exit(__doc__)
    except DropboxError as e:
        if "invalid_client" in str(e):
            sys.exit(f"Dropbox said: {e}\nCheck the app key and secret on the app's Settings tab at {APP_CONSOLE}.")
        if "missing_scope" in str(e):
            sys.exit(f"Dropbox said: {e}\nTick the missing permission on the app's Permissions tab at {APP_CONSOLE}, "
                     "click Submit, then run this setup again and link Dropbox again.")
        sys.exit(f"Dropbox said: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"Couldn't reach Dropbox: {e.reason}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nStopped, because this failed: {' '.join(e.cmd)}")
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
