#!/usr/bin/env python3
"""
Interactive setup: links your Dropbox app, writes the .env file and checks that
Dropbox can be reached. Needs only Python 3, nothing to install.

    ./setup.py                  asks which part to set up
    ./setup.py backup [dir]     back up a folder to Dropbox, or restore one from it
                                (dir: backup-docker, or a copy of it for another folder)
    ./setup.py mcp              the Dropbox MCP server (dropbox-mcp)
    ./setup.py check [part]     test the saved settings (part: backup, mcp or a directory)
    ./setup.py shared [part]    list your shared folders and the path to use for each
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backup-docker")
MCP_DIR = os.path.join(HERE, "dropbox-mcp")
APP_CONSOLE = "https://www.dropbox.com/developers/apps"

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


def api(token, route, body=None, team_root=None):
    """Call a Dropbox API route, with paths relative to the team's root folder if `team_root` is given."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if team_root:
        headers["Dropbox-API-Path-Root"] = json.dumps({".tag": "root", "root": team_root})
    return post(f"https://api.dropboxapi.com/2/{route}", json.dumps(body).encode(), headers)


def connect(env):
    """An access token from the saved refresh token, and the account it belongs to."""
    token = oauth(env, grant_type="refresh_token", refresh_token=env["DROPBOX_REFRESH_TOKEN"])["access_token"]
    return token, api(token, "users/get_current_account")


def folder_problem(token, path, team_root=None):
    """None if `path` is a folder in Dropbox, otherwise Dropbox's complaint (e.g. "path/not_found/")."""
    try:
        api(token, "files/list_folder", {"path": "" if path == "/" else path, "limit": 1}, team_root)
    except DropboxError as e:
        return str(e)


def ask_app(env):
    """Ask for the Dropbox app's key and secret, offering the ones the other part uses."""
    others = {**read_env(os.path.join(BACKUP_DIR, ".env")), **read_env(os.path.join(MCP_DIR, ".env"))}
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
        note = "" if folder.get("path_display") else ", not added to your Dropbox"
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
        if not remote.startswith(("/", "ns:")):
            remote = "/" + remote
        remote = remote.rstrip("/") or "/"
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
    where = os.path.relpath(folder)
    print(f"\nWrote {os.path.relpath(env_file)}.\n")
    print(f"Start it (or apply new settings), from {where}:\n  docker compose up -d --build\n")
    print("See what it's doing:\n  docker compose logs -f")


def setup_mcp():
    """Set up dropbox-mcp: writable folders, team folders, and the Dropbox link."""
    env_file = os.path.join(MCP_DIR, ".env")
    env = read_env(env_file)
    print("Dropbox MCP setup. Press Enter to keep the value in brackets.\n")
    ask_app(env)

    print("\nMCP clients can read all of Dropbox, and write only inside the folders you list here.")
    print("Use paths like /Shared/Team, or ns:<id> for shared folders that aren't in your Dropbox")
    print("(./setup.py shared lists them). Enter - for none.")
    was_writable = bool(env.get("WRITABLE_PATHS"))
    answer = ask("Writable folders, comma-separated", env.get("WRITABLE_PATHS", "-"))
    env["WRITABLE_PATHS"] = "" if answer == "-" else ",".join(p.strip() for p in answer.split(",") if p.strip())
    if env["WRITABLE_PATHS"]:
        env["WRITE_PERMISSION"] = ask("Auth0 permission a user needs to write", env.get("WRITE_PERMISSION") or "dropbox:write")
    else:
        env["WRITE_PERMISSION"] = env.get("WRITE_PERMISSION") or "dropbox:write"
    team = choose("\nIs this a Dropbox Business account, and should team folders show at the top level?",
                  ["y", "n"], "y" if env.get("DROPBOX_TEAM_SPACE") == "true" else "n")
    env["DROPBOX_TEAM_SPACE"] = "true" if team == "y" else "false"

    scopes = MCP_SCOPES + (" files.content.write" if env["WRITABLE_PATHS"] else "")
    relink = bool(env.get("DROPBOX_REFRESH_TOKEN")) and was_writable != bool(env["WRITABLE_PATHS"])
    if relink:
        print("\nThe writable folders changed, so Dropbox needs linking again with matching permissions.")
    token, account = link_dropbox(env, scopes, relink)

    team_root = account["root_info"]["root_namespace_id"] if team == "y" else None
    for path in filter(None, env["WRITABLE_PATHS"].split(",")):
        problem = folder_problem(token, path, team_root)
        if problem and "not_folder" not in problem:
            note = " (writing a file there creates it)" if "not_found" in problem else ""
            print(f"  Writable folder {path}: {problem}{note}")

    env["MCP_HOST"] = ask("\nPublic hostname for the MCP server, e.g. dropbox-mcp.example.com", env.get("MCP_HOST", ""))
    write_env(env_file, env, ["APP_KEY", "APP_SECRET", "DROPBOX_REFRESH_TOKEN", "WRITABLE_PATHS", "WRITE_PERMISSION",
                              "DROPBOX_TEAM_SPACE", "MCP_HOST"])
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
        print(f"  Not set up yet. Run: ./setup.py {'mcp' if is_mcp else 'backup'}")
        return False
    try:
        token, account = connect(env)
    except DropboxError as e:
        print(f"  Dropbox link: FAILED ({e}). Run ./setup.py {'mcp' if is_mcp else 'backup'} again.")
        return False
    print(f"  Dropbox link: OK ({account['email']})")
    ok = True
    if is_mcp:
        team_root = account["root_info"]["root_namespace_id"] if env.get("DROPBOX_TEAM_SPACE") == "true" else None
        for path in filter(None, env.get("WRITABLE_PATHS", "").split(",")):
            problem = folder_problem(token, path, team_root)
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
    args = sys.argv[1:]
    target = args[1] if len(args) > 1 else None
    try:
        command = args[0] if args else choose("Set up which part: backup (back up or restore a folder) or mcp?",
                                              ["backup", "mcp"], "backup")
        if command == "backup":
            setup_backup(os.path.abspath(target) if target else BACKUP_DIR)
        elif command == "mcp":
            setup_mcp()
        elif command == "check":
            dirs = part_dirs(target)
            if not dirs:
                sys.exit("Nothing is set up yet. Run ./setup.py backup or ./setup.py mcp.")
            results = [check(d) for d in dirs]
            sys.exit(0 if all(results) else 1)
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
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nStopped. Nothing was saved.")


if __name__ == "__main__":
    main()
