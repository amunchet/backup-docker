# Backup Docker

Two small Docker services for working with Dropbox from a server:

- **[backup-docker](#part-a-back-up-or-restore-a-folder)** keeps a folder on this machine in sync with a Dropbox folder. It either **backs up** the folder to Dropbox, or **restores** (downloads) a Dropbox folder onto this machine, shared folders included. It can also put a file back to an earlier version ("time travel").
- **[dropbox-mcp](#part-b-dropbox-for-mcp-clients)** lets AI clients such as LibreChat read your Dropbox, shared folders included, over MCP. Everything is read-only except the folders you choose. It runs behind [caddy-auth0](https://github.com/amunchet/caddy-auth0), which handles login.

A setup script, `./setup.py`, does the fiddly parts: it links Dropbox, writes the settings file and checks that everything can be reached.

**Contents:** [What you need](#what-you-need) · [Step 1: Create a Dropbox app](#step-1-create-a-dropbox-app) · [Part A: Back up or restore a folder](#part-a-back-up-or-restore-a-folder) · [Part B: Dropbox for MCP clients](#part-b-dropbox-for-mcp-clients) · [Troubleshooting](#troubleshooting) · [Development](#development)

## What you need

- A Linux machine with **Docker** and **Docker Compose**. `docker compose version` should print a version.
- **git**, and **Python 3** for the setup script. Setup needs nothing else installed.
- A **Dropbox** account.
- For Part B only: [caddy-auth0](https://github.com/amunchet/caddy-auth0) running on the same machine.

Get the code:

```sh
git clone https://github.com/amunchet/backup-docker
cd backup-docker
```

Commands below are run from this folder unless they say otherwise.

## Step 1: Create a Dropbox app

Both parts reach Dropbox through a Dropbox "app": a private key to the Dropbox API that you create for yourself. One app is enough for both parts, and you only do this once.

1. Go to <https://www.dropbox.com/developers/apps> and sign in with the Dropbox account whose files you want to use.
2. Click **Create app**.
3. Under **Choose an API**, pick **Scoped access**.
4. Under **Choose the type of access you need**, pick **Full Dropbox**. An "App folder" app only sees a folder of its own, not your existing files or shared folders.
5. Give the app a name that isn't taken, e.g. `yourname-backup`, and click **Create app**.
6. Open the **Permissions** tab. Tick these five permissions (some may already be ticked), then click **Submit** at the bottom of the page:
   - `account_info.read`
   - `files.metadata.read`
   - `files.content.read`
   - `files.content.write`
   - `sharing.read`

   Do this before linking. Dropbox only grants permissions the app had at the time it was linked. Ticking `files.content.write` doesn't give the MCP server write access; setup only asks for it there if you choose writable folders.
7. Open the **Settings** tab and leave it open. Setup asks for the **App key** and **App secret** shown there; click **Show** to reveal the secret.

You don't need a redirect URI, and the app can stay in "Development" status.

## Part A: Back up or restore a folder

backup-docker runs in the background and makes a pass every few minutes:

- **Backup** uploads files that are new or changed since the last pass, subfolders included, to the same place in the Dropbox folder.
- **Restore** downloads files that are new or changed in Dropbox since the last pass, subfolders included, into the local folder. It can restore from a shared folder.

Neither direction ever deletes anything. Dropbox also keeps old versions, which [time travel](#time-travel) can bring back.

### A1. Run setup

```sh
./setup.py backup
```

It asks these questions. Press Enter to keep the value shown in brackets.

| Question | What to answer |
| --- | --- |
| Dropbox app key, app secret | From the app's **Settings** tab (step 1.7). |
| *Link Dropbox* | Open the address it prints, while signed in to Dropbox. Click **Continue**, then **Allow**, and paste the access code Dropbox shows. |
| Back up or restore? | `backup` to copy a folder here to Dropbox. `restore` to copy a Dropbox folder here. |
| Folder on this machine | A full path, e.g. `/srv/data`. For a backup it must exist. For a restore it's created if needed, and files in it that also exist in Dropbox get replaced by the Dropbox version. |
| Dropbox folder | Where in Dropbox, e.g. `/Backups/server1`. A backup creates it if needed. For a restore, setup first lists your shared folders and the path to use for each; see [Restoring a shared folder](#restoring-a-shared-folder). |
| Seconds between passes | `300` (5 minutes) suits most uses. |

Setup saves your answers in `backup-docker/.env`, readable only by you, and checks that Dropbox and both folders can be reached. You can run it again at any time to change something. Your current answers become the defaults.

### A2. Start it

```sh
cd backup-docker
docker compose up -d --build
docker compose logs -f
```

The logs should show something like this. `Ctrl+C` stops following the logs; the container keeps running.

```
[DROPBOX-BACKUP INFO] ... BACKUP: /backup -> Dropbox /Backups/server1 (you@example.com), every 300s
[DROPBOX-BACKUP INFO] ... Uploading changed file: reports/q3.xlsx -> /Backups/server1/reports/q3.xlsx
[DROPBOX-BACKUP INFO] ... Backup pass done: 1 uploaded, 0 failed, 41 unchanged.
```

It keeps running after reboots until you stop it with `docker compose down`.

### Where things are (Docker paths)

The container only sees the one folder you chose, mounted inside it as `/backup`. That's why the logs say `/backup`. You never type `/backup` yourself.

| On this machine | Inside the container | What it is |
| --- | --- | --- |
| The folder you chose (`WATCH_FOLDER` in `.env`), e.g. `/srv/data` | `/backup` | The folder being backed up or restored into |
| `backup-docker/.env` | environment variables | Your settings, passed in when the container starts. They're not part of the image. |
| `backup-docker/post-download-hook.sh`, if you create one | `/app/post-download-hook.sh` | Copied in when the image is built, so rebuild after changing it |
| `checksums.txt`, `ignore.txt`, `.restore-state.json` in your folder | the same, under `/backup` | Bookkeeping, explained below. These files are never synced. |

### Everyday commands

Run these in `backup-docker/`.

| To | Run |
| --- | --- |
| See what it's doing | `docker compose logs -f` |
| Stop it | `docker compose down` |
| Start it again, or apply changed settings | `docker compose up -d --build` |
| Change settings | `../setup.py backup`, then `docker compose up -d --build` |
| Check the settings still work | `../setup.py check` |
| Update to the latest version | `git pull`, then `docker compose up -d --build` |

### How backups work

- **Only changes are uploaded.** Each file's checksum is saved in `checksums.txt` in your folder. Each pass uploads only files whose checksum changed, replacing the copy in Dropbox. Files over 4 MB go up in pieces, so large files work too.
- **Nothing is deleted from Dropbox.** A file you delete here stays in Dropbox.
- **Failed uploads are retried** on the next pass, e.g. after a network outage.
- **Skipping files.** Put glob patterns in `ignore.txt` in your folder, one per line. Lines starting with `#` are comments.
  ```
  *.tmp
  cache/*
  node_modules/*
  ```
  Patterns match the path relative to your folder, and `*` also matches `/`.

### How restores work

- **The whole folder comes down**, subfolders included, with the same structure.
- **Only changes are downloaded.** Dropbox reports a hash of each file's content. A file is downloaded only if that changed since it was last downloaded, which is remembered in `.restore-state.json` in your folder. A file that's already identical here is never downloaded.
- **Local edits are kept until Dropbox's copy changes.** Edits by you or the hook below stay, and the next change in Dropbox replaces them. Delete a local file and it's downloaded again on the next pass.
- **Nothing is deleted here.** A file deleted from Dropbox stays here.
- **Files appear complete.** Each download goes to a hidden temporary file first and is swapped in when done.
- **Skipped:** files matching `ignore.txt` (as for backups), and Dropbox Paper or Google docs, which Dropbox can't download as files.
- **API use:** each pass lists every folder. For a tree with hundreds of folders, use a longer interval, e.g. 900 seconds.

### Restoring a shared folder

A folder someone shared with you can be restored like any other. What to use as the Dropbox folder depends on whether it's added to your Dropbox:

- **Added to your Dropbox**, so it shows in your file list: use its path, e.g. `/Team Docs`.
- **Not added**, or a team folder in a Dropbox Business team space: use `ns:<id>`, e.g. `ns:1234567890`.

`./setup.py shared` lists every shared folder you can reach, with the path to use for each:

```
Shared folders (use the path on the left):
  /Team Docs                               Team Docs (editor)
  ns:1234567890                            Client Files (viewer, not added to your Dropbox)
```

Setup also shows this list when you choose `restore`. You can back up *into* a shared folder too, if you have edit access.

### Post-download hook

To process each restored file, e.g. to fix up a database dump before importing it, create `backup-docker/post-download-hook.sh`. It runs after each download, with the file's path as `$1`:

```sh
cp backup-docker/post-download-hook.sh-example backup-docker/post-download-hook.sh
# edit it, then rebuild so the container gets the new version:
cd backup-docker && docker compose up -d --build
```

A failing hook is logged and the restore carries on. The hook's own edits are kept; see [How restores work](#how-restores-work).

### Time travel

Dropbox keeps earlier versions of files, by default for 30 days or longer depending on your plan. `time_travel.py` puts a file back to the version it had on a given date. It changes nothing unless you add `--execute`.

```sh
cd backup-docker
# Which version would be restored? (newest one saved on or before the date)
docker compose exec dropbox-backup python time_travel.py "/Backups/server1/db.sql" --date 2025-09-01
# List the 20 newest versions from on or before that date
docker compose exec dropbox-backup python time_travel.py "/Backups/server1/db.sql" --date 2025-09-01 --list 20
# Restore it
docker compose exec dropbox-backup python time_travel.py "/Backups/server1/db.sql" --date 2025-09-01 --execute
```

The path is the file's path in Dropbox, not on this machine. `--date` also takes a full time such as `2025-09-01T14:30:00+02:00`. `--size <bytes>` keeps only versions of exactly that size, and `--nth 2` picks the second-newest match. The restored version becomes the newest one in Dropbox, so a restore setup downloads it on its next pass.

### Syncing more than one folder

Each copy of `backup-docker` syncs one folder. For another folder, make a copy whose name starts with `backup-docker-`; git ignores these copies. Then set the copy up and start it:

```sh
cp -r backup-docker backup-docker-photos
rm -f backup-docker-photos/.env
./setup.py backup backup-docker-photos
cd backup-docker-photos && docker compose up -d --build
```

After `git pull`, copy the updated files into each copy, leaving its `.env` alone, and rebuild it:

```sh
cd backup-docker && cp *.py Dockerfile docker-compose.yml requirements.txt ../backup-docker-photos/
cd ../backup-docker-photos && docker compose up -d --build
```

### Running without Docker

```sh
cd backup-docker
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py --once    # one pass, to try it out
```

Without Docker, `WATCH_FOLDER` is used as is; setup always saves a full path. To keep it running as a systemd service, run this from `backup-docker/`. It fills in your user and this folder:

```sh
sudo tee /etc/systemd/system/dropbox-backup.service >/dev/null <<EOF
[Unit]
Description=Dropbox backup/restore (backup-docker)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/venv/bin/python main.py
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now dropbox-backup
journalctl -u dropbox-backup -f    # follow the logs
```

Or run single passes from cron, e.g. every 15 minutes:

```
*/15 * * * * cd /path/to/backup-docker && venv/bin/python main.py --once
```

A PID file in the folder stops two copies of the same folder running at once.

### Settings reference (`backup-docker/.env`)

| Setting | Meaning | Default |
| --- | --- | --- |
| `APP_KEY`, `APP_SECRET` | The Dropbox app's key and secret | (setup) |
| `DROPBOX_REFRESH_TOKEN` | The Dropbox link; setup gets it | (setup) |
| `SYNC_DIRECTION` | `BACKUP`: this folder goes to Dropbox. `RESTORE`: the Dropbox folder comes here. | `BACKUP` |
| `WATCH_FOLDER` | The folder on this machine. Docker mounts it at `/backup`. | required |
| `DROPBOX_FOLDER` | The Dropbox folder: `/path`, `ns:<id>` for a shared folder, or `/` for all of Dropbox | required |
| `SLEEP_TIME` | Seconds between passes | `60` |
| `DEBUG_LEVEL` | `DEBUG` logs every skipped and unchanged file | `INFO` |
| `LOG_FILE` | Also write the log to this file, which rotates at 10 MB. In Docker the path is inside the container. | none |

## Part B: Dropbox for MCP clients

dropbox-mcp is an MCP server (Streamable HTTP, at `/mcp`) that gives clients such as LibreChat these tools:

| Tool | What it does |
| --- | --- |
| `account_info` | The Dropbox account, who is calling, and which folders are writable |
| `list_folder` | A folder's contents, optionally everything below it |
| `search` | Search file and folder names and contents, optionally under one folder |
| `get_metadata` | Details of one file or folder |
| `read_file` | A file's text, or the image itself for images, or base64 for anything else (up to 10 MB) |
| `list_shared_folders` | Every shared folder the account can reach, including ones not added to it |
| `write_file` | Create a file, or replace one with `overwrite`, inside a writable folder. The only tool that changes anything. |

The server has no login of its own. caddy-auth0 sits in front of it: it checks each caller's token and passes on who they are and their Auth0 permissions. Every caller sees the same Dropbox account, the one you link in setup. Auth0 decides who may use the server, not whose Dropbox it opens.

### B1. Have caddy-auth0 running

Follow [its README](https://github.com/amunchet/caddy-auth0) until its demo site works. dropbox-mcp joins its `caddy-auth0` Docker network.

### B2. Run setup

```sh
./setup.py mcp
```

| Question | What to answer |
| --- | --- |
| Dropbox app key, app secret | From the app's **Settings** tab. If Part A is set up, its values are offered. |
| Writable folders | Usually `-`, meaning none. Otherwise, the folders MCP clients may write to, comma-separated. See [Reading and writing](#reading-and-writing). |
| Auth0 permission to write | Asked only if something is writable. `dropbox:write` is fine. |
| Dropbox Business team folders at the top level? | `y` only for a Dropbox Business account whose team folders should be browsable from the top. |
| *Link Dropbox* | As in Part A. The link only gets write access if you listed writable folders. |
| Public hostname | e.g. `dropbox-mcp.example.com`. Setup uses it to print your Caddy and client settings. |

Setup writes `dropbox-mcp/.env` and prints the next steps with your hostname filled in. They are:

### B3. Add it to Caddy and start it

1. Point DNS for the hostname at this machine.
2. Add the site to caddy-auth0's `Caddyfile`:
   ```caddyfile
   dropbox-mcp.example.com {
   	auth0 {
   		mcp
   		require mcp:dropbox
   	}
   	reverse_proxy dropbox-mcp:8000
   }
   ```
3. Start the server, then reload Caddy:
   ```sh
   cd dropbox-mcp && docker compose up -d --build
   # in the caddy-auth0 folder:
   docker compose exec -w /etc/caddy caddy caddy reload
   ```

The server publishes no ports, so Caddy is the only way in.

### B4. Give access

Callers need the `mcp:dropbox` permission to use the server at all. To write, they also need `dropbox:write` (`WRITE_PERMISSION`). Either:

- **People via Auth0:** in Auth0, under *Applications → APIs → your API → Permissions*, add `mcp:dropbox` and, if needed, `dropbox:write`. Grant them to users or roles under *User Management*.
- **A token for one client** (a script, or a LibreChat instance): in the caddy-auth0 folder, run
  ```sh
  ./mcp-token dropbox-mcp librechat                                # read-only
  ./mcp-token dropbox-mcp librechat -p mcp:dropbox,dropbox:write   # can also write
  ```
  It prints a token that's valid for 90 days.

### B5. Connect LibreChat

With a token from `./mcp-token`, add this to `librechat.yaml` and put the token in LibreChat's `.env` as `DROPBOX_MCP_TOKEN`:

```yaml
mcpServers:
  dropbox:
    type: streamable-http
    url: https://dropbox-mcp.example.com/mcp
    requiresOAuth: false
    headers:
      Authorization: "Bearer ${DROPBOX_MCP_TOKEN}"
```

To have each LibreChat user connect as themselves instead, see "Connecting LibreChat" in the caddy-auth0 README.

### Reading and writing

- **Paths** look like `/Folder/file.txt`, and `""` is the top of your Dropbox. A shared folder that isn't added to your Dropbox is `ns:<id>/...`; `list_shared_folders` and `./setup.py shared` show the ids. With team folders at the top level (`DROPBOX_TEAM_SPACE=true`), paths start at the team's root, and your own files are under `/<your name>`.
- **Everything is read-only** except inside `WRITABLE_PATHS`. Matching ignores case, as Dropbox does, and paths with `..` are refused.
- **A folder has two forms.** A shared folder added to your Dropbox can be written to by its path (`/Team Docs`) only if that form is listed, and by its `ns:` form only if that form is listed.
- **A write needs both** a writable path and the caller's `dropbox:write` permission. Caddy can't tell reads from writes, because every MCP call is a `POST /mcp`, so the server checks the permission itself.
- **Nothing writable** means setup links Dropbox without write access at all. Even the server's own Dropbox link can't change anything.

### Settings reference (`dropbox-mcp/.env`)

| Setting | Meaning | Default |
| --- | --- | --- |
| `APP_KEY`, `APP_SECRET`, `DROPBOX_REFRESH_TOKEN` | As in Part A | (setup) |
| `WRITABLE_PATHS` | Comma-separated folders or files clients may write to | none |
| `WRITE_PERMISSION` | Auth0 permission needed to write | `dropbox:write` |
| `DROPBOX_TEAM_SPACE` | `true` to browse from a Dropbox Business team's root | `false` |
| `MAX_READ_BYTES` | Largest file `read_file` returns | `10485760` (10 MB) |
| `LIST_LIMIT` | Most entries `list_folder` returns | `2000` |
| `MCP_HOST` | The public hostname; only setup uses it | none |

## Troubleshooting

`./setup.py check` tests the saved settings of both parts: the Dropbox link and the folders.

| Symptom | Fix |
| --- | --- |
| Dropbox shows *invalid_scope* when you open the link | The app is missing a permission. Tick all five in [step 1.6](#step-1-create-a-dropbox-app), click **Submit**, and run setup again. |
| Setup or the logs say *missing_scope* | The same, then run setup again and answer `y` to *Link again?*. |
| Setup says *Check the app key and secret* | Copy both again from the app's **Settings** tab. Click **Show** for the secret. |
| Setup says *The saved Dropbox link doesn't work any more* | The link was removed, e.g. under *Settings → Connected apps* in Dropbox. Link again when setup asks. |
| `docker compose up` says *Set WATCH_FOLDER in .env* | Run `../setup.py backup` first, from the same folder. |
| The logs say *SYNC_DIRECTION must be BACKUP or RESTORE* | Fix the spelling in `.env`, or run setup again. |
| A restore of a shared folder finds nothing | If the folder isn't added to your Dropbox, use its `ns:` path from `./setup.py shared`. |
| Restored files belong to root | Docker runs as root. Use `chown`, or [run without Docker](#running-without-docker) as your own user. |
| Changes to `post-download-hook.sh` have no effect | Rebuild: `docker compose up -d --build`. |
| The logs say *Failed to upload ... insufficient_space* | Your Dropbox is full. Failed files are retried every pass. |
| `docker compose up` for dropbox-mcp says *network caddy-auth0 ... could not be found* | Start caddy-auth0 first. |
| `write_file` says *is read-only* | Add the folder to the writable folders with `./setup.py mcp`, then `docker compose up -d` in `dropbox-mcp`. Check whether you listed its `/path` or `ns:` form. |
| `write_file` says *Writing needs the 'dropbox:write' permission* | Grant `dropbox:write` in Auth0, or mint a token with `-p mcp:dropbox,dropbox:write`. |
| MCP clients get `401` or `403` | See the caddy-auth0 README's troubleshooting. `./mcp-token check <token>` tests a token. |

## Development

```sh
python3 -m venv venv
venv/bin/pip install -r backup-docker/requirements.txt -r dropbox-mcp/requirements.txt
PATH=$PWD/venv/bin:$PATH ./run_tests.sh
```

`run_tests.sh` runs the setup tests (`tests/`), then each part's own tests. Those can also run alone: `backup-docker/run_tests.sh`, which requires 95% coverage, and `dropbox-mcp/run_tests.sh`.

```
setup.py                  interactive setup for both parts
run_tests.sh              runs every test suite
tests/                    tests for setup.py
backup-docker/
  main.py                 runs a backup or restore pass every SLEEP_TIME seconds
  backup.py, restore.py   the two directions
  time_travel.py          puts a file back to an earlier version
  common.py               settings, logging and the Dropbox client
  post-download-hook.sh-example
  Dockerfile, docker-compose.yml, .env.sample, tests/
dropbox-mcp/
  server.py               the MCP server
  Dockerfile, docker-compose.yml, .env.sample, tests/
CHANGELOG/                one change log per working branch
```
