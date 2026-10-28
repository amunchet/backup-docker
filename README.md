# Backup Docker

Two small services for working with Dropbox from a server:

- **[backup-docker](#part-a-back-up-or-restore-a-folder)** keeps a folder on this machine in sync with a Dropbox folder. It either **backs up** the folder to Dropbox, or **restores** (downloads) a Dropbox folder onto this machine, shared folders included. It can also put a file back to an earlier version ("time travel"). It runs in Docker, or as a systemd service without Docker.
- **[dropbox-mcp](#part-b-dropbox-for-mcp-clients)** lets AI clients such as LibreChat read your Dropbox, shared folders included, over MCP. Everything is read-only except the folders you choose. It runs behind [caddy-auth0](https://github.com/amunchet/caddy-auth0), which handles login.

A setup script, `./setup.py`, does the fiddly parts: it links Dropbox, writes the settings file and checks that everything can be reached. It also installs backup-docker as a systemd service, if you'd rather not use Docker.

**Contents:** [What you need](#what-you-need) · [Step 1: Create a Dropbox app](#step-1-create-a-dropbox-app) · [Part A: Back up or restore a folder](#part-a-back-up-or-restore-a-folder) · [Part B: Dropbox for MCP clients](#part-b-dropbox-for-mcp-clients) · [Troubleshooting](#troubleshooting) · [Development](#development)

## What you need

- A Linux machine with **Docker** and **Docker Compose**. `docker compose version` should print a version.
  - For Part A, systemd can take Docker's place: backup-docker can [run as a systemd service](#a3-or-run-it-as-a-systemd-service). Most Linux distributions use systemd.
- **git**, and **Python 3** for the setup script. Setup needs nothing else installed. The systemd service needs Python 3.8 or newer with its `venv` module; on Debian or Ubuntu, get that with `sudo apt install python3-venv`.
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

You can run it with Docker ([A1](#a1-run-setup), then [A2](#a2-start-it-with-docker)), or as a systemd service ([A3](#a3-or-run-it-as-a-systemd-service)). Pick one for each folder, never both, so the folder isn't synced twice at once.

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

### A2. Start it with Docker

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

### A3. Or: run it as a systemd service

Instead of Docker, backup-docker can run as an ordinary systemd service. It runs as your user, so restored files belong to you, and it can back up only files you can read. It starts at boot, and systemd restarts it a minute after a crash. One command sets everything up, so skip A1 and A2.

1. On Debian or Ubuntu, install Python's `venv` module. Other distributions include it.
   ```sh
   sudo apt install python3-venv
   ```
2. Create the Dropbox app ([step 1](#step-1-create-a-dropbox-app)) if you haven't yet.
3. Run the installer as the user the service should run as. Don't put `sudo` in front; it asks for your password when it needs it.
   ```sh
   ./setup.py service
   ```

It works through these steps, and stops with an explanation if something is wrong:

| Step | What happens |
| --- | --- |
| Checks | systemd runs this machine, `sudo` works, Python is 3.8 or newer, and this folder isn't also running in Docker. |
| 1. Python | Makes `backup-docker/venv`, a private Python environment for the service. |
| 2. Settings | If `backup-docker/.env` works, shows it and asks whether to keep it. Otherwise it asks the [setup questions](#a1-run-setup). Then it checks that you can read and write the folder. |
| 3. Packages | Installs the service's Python packages into the venv. |
| 4. Service | Writes `/etc/systemd/system/dropbox-backup.service`, enables and starts it, and waits until it has connected to Dropbox. |

It ends like this:

```
[4/4] Installing and starting /etc/systemd/system/dropbox-backup.service (sudo may ask for your password)
  | Started Dropbox backup/restore (/home/you/backup-docker/backup-docker).
  | [DROPBOX-BACKUP INFO] ... BACKUP: /srv/data -> Dropbox /Backups/server1 (you@example.com), every 300s
  | [DROPBOX-BACKUP INFO] ... Backup pass done: 42 uploaded, 0 failed, 0 unchanged.

dropbox-backup is running. It starts by itself at boot.

  See what it's doing:  sudo journalctl -u dropbox-backup -f
  Is it running?        sudo systemctl status dropbox-backup
  Apply an edited .env: sudo systemctl restart dropbox-backup
  Change settings:      ./setup.py service
  Remove the service:   ./setup.py remove-service
```

If the service doesn't start, you see its log instead, saying why, e.g. *Dropbox refused the saved settings*. Fix that and run `./setup.py service` again. Running it again is always safe: it keeps the settings and venv if they work, and restarts the service. Use it to change settings, and after updating.

<details>
<summary>The service file it writes</summary>

`./setup.py service` writes this file each time, with your user and paths filled in, so there's no need to edit it.

```ini
[Unit]
Description=Dropbox backup/restore (/home/you/backup-docker/backup-docker)
Wants=network-online.target
After=network-online.target

[Service]
User=you
WorkingDirectory=/home/you/backup-docker/backup-docker
ExecStart="/home/you/backup-docker/backup-docker/venv/bin/python" "/home/you/backup-docker/backup-docker/main.py"
Restart=on-failure
RestartSec=60
SyslogIdentifier=dropbox-backup

[Install]
WantedBy=multi-user.target
```
</details>

### Where things are (Docker paths)

The container only sees the one folder you chose, mounted inside it as `/backup`. That's why the logs say `/backup`. You never type `/backup` yourself.

| On this machine | Inside the container | What it is |
| --- | --- | --- |
| The folder you chose (`WATCH_FOLDER` in `.env`), e.g. `/srv/data` | `/backup` | The folder being backed up or restored into |
| `backup-docker/.env` | environment variables | Your settings, passed in when the container starts. They're not part of the image. |
| `backup-docker/post-download-hook.sh`, if you create one | `/app/post-download-hook.sh` | Copied in when the image is built, so rebuild after changing it |
| `checksums.txt`, `ignore.txt`, `.restore-state.json` in your folder | the same, under `/backup` | Bookkeeping, explained below. These files are never synced. |

The systemd service has no mapping: it uses your folder and `post-download-hook.sh` where they are, and logs to the system journal.

### Everyday commands

Run the Docker commands in `backup-docker/`. The service commands work anywhere, except `./setup.py`, which runs in the project folder.

| To | With Docker | As a systemd service |
| --- | --- | --- |
| See what it's doing | `docker compose logs -f` | `sudo journalctl -u dropbox-backup -f` |
| See whether it's running | `docker compose ps` | `sudo systemctl status dropbox-backup` |
| Stop it | `docker compose down` | `sudo systemctl stop dropbox-backup` (it starts again at boot) |
| Start it again | `docker compose up -d --build` | `sudo systemctl start dropbox-backup` |
| Change settings | `../setup.py backup`, then `docker compose up -d --build` | `./setup.py service` |
| Apply a hand-edited `.env` | `docker compose up -d` | `sudo systemctl restart dropbox-backup` |
| Check the settings still work | `../setup.py check` | `./setup.py check` |
| Update to the latest version | `git pull`, then `docker compose up -d --build` | `git pull`, then `./setup.py service` |
| Remove it | `docker compose down` | `./setup.py remove-service` |

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

A folder someone shared with you, or a team folder, can be restored like any other. What to use as the Dropbox folder depends on where it is:

- **In your own folder**, so it shows in your file list: use its path, e.g. `/Team Docs`.
- **Outside it**: a team folder, or a shared folder that isn't added to your Dropbox. Use `ns:<id>`, e.g. `ns:1234567890`, or `ns:1234567890/Drawings` for a folder inside it. Dropbox can't find these by a path from your own folder, so an `ns:` path makes each request start at that folder instead.

`./setup.py shared` lists every shared folder you can reach, with the path to use for each:

```
Shared folders (use the path on the left):
  /Team Docs                               Team Docs (editor)
  ns:1234567890                            Client Files (viewer, not added to your Dropbox)
  ns:2345678901                            Engineering (editor, team folder)
```

Setup also shows this list when you choose `restore`. You can back up *into* a shared folder too, if you have edit access. Dropbox only lets apps write into a shared folder once it's added to your Dropbox, so add it first (on dropbox.com, under Shared). Team folders don't need adding.

### Post-download hook

To process each restored file, e.g. to fix up a database dump before importing it, create `backup-docker/post-download-hook.sh`. It runs after each download, with the file's path as `$1`:

```sh
cp backup-docker/post-download-hook.sh-example backup-docker/post-download-hook.sh
# edit it, then rebuild so the container gets the new version:
cd backup-docker && docker compose up -d --build
```

A systemd service needs no rebuild: it uses the hook as soon as you save it.

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

With the systemd service, run it with the service's Python instead, in `backup-docker/`: `venv/bin/python time_travel.py "/Backups/server1/db.sql" --date 2025-09-01`.

The path is the file's path in Dropbox, not on this machine. `--date` also takes a full time such as `2025-09-01T14:30:00+02:00`. `--size <bytes>` keeps only versions of exactly that size, and `--nth 2` picks the second-newest match. The restored version becomes the newest one in Dropbox, so a restore setup downloads it on its next pass.

### Syncing more than one folder

Each copy of `backup-docker` syncs one folder. For another folder, make a copy whose name starts with `backup-docker-`; git ignores these copies. Then set the copy up and start it:

```sh
cp -r backup-docker backup-docker-photos
rm -f backup-docker-photos/.env
./setup.py backup backup-docker-photos
cd backup-docker-photos && docker compose up -d --build
```

For a systemd service, run `./setup.py service backup-docker-photos` instead of the last two lines. That copy's service is called `dropbox-backup-photos`.

After `git pull`, copy the updated files into each copy, leaving its `.env` alone, and rebuild it. For a service, run `./setup.py service backup-docker-photos` instead of the last line.

```sh
cd backup-docker && cp *.py Dockerfile docker-compose.yml requirements.txt ../backup-docker-photos/
cd ../backup-docker-photos && docker compose up -d --build
```

### Single passes, e.g. from cron

`main.py --once` makes one pass and exits, for running from cron instead of as a service. After `./setup.py backup`, make the Python environment once and try a pass:

```sh
cd backup-docker
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py --once
```

Then add a line like this with `crontab -e`, for a pass every 15 minutes:

```
*/15 * * * * /path/to/backup-docker/venv/bin/python /path/to/backup-docker/main.py --once
```

Only one copy runs at a time for each folder. A second copy, e.g. a cron pass while the last one is still going, or a manual run while the service runs, stops straight away with *already running*.

### Settings reference (`backup-docker/.env`)

| Setting | Meaning | Default |
| --- | --- | --- |
| `APP_KEY`, `APP_SECRET` | The Dropbox app's key and secret | (setup) |
| `DROPBOX_REFRESH_TOKEN` | The Dropbox link; setup gets it | (setup) |
| `SYNC_DIRECTION` | `BACKUP`: this folder goes to Dropbox. `RESTORE`: the Dropbox folder comes here. | `BACKUP` |
| `WATCH_FOLDER` | The folder on this machine. Docker mounts it at `/backup`; the systemd service uses it directly. | required |
| `DROPBOX_FOLDER` | The Dropbox folder: `/path`, `ns:<id>` or `ns:<id>/path` for a team folder or a shared folder outside your own ([details](#restoring-a-shared-folder)), or `/` for all of Dropbox | required |
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
| `list_shared_folders` | Every shared folder the account can reach, including ones not added to it. Not offered when clients can read only one folder. |
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
| Folder MCP clients can read | `/` for all of Dropbox. Or the one folder they may read, which becomes their top level: a path like `/Projects`, or `ns:<id>/path` for a team folder or a shared folder that isn't in your Dropbox (`./setup.py shared` lists the ids). |
| Writable folders | Usually `-`, meaning none. Otherwise, the folders MCP clients may write to, comma-separated, and inside the folder above if you gave one, from it (`/Outbox`) or in full (`ns:<id>/Outbox`). See [Reading and writing](#reading-and-writing). |
| Auth0 permission to write | Asked only if something is writable. `dropbox:write` is fine. |
| Dropbox Business team folders at the top level? | Asked only when clients can read all of Dropbox. `y` only for a Dropbox Business account whose team folders should be browsable from the top. |
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

The server publishes no ports, so Caddy is the only way in. That's also why it runs only in Docker, not as a systemd service: outside Docker it would listen on a port of this machine, and anything that could reach that port would get in without Auth0.

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

- **Paths** look like `/Folder/file.txt`, and `""` is the top of your Dropbox. A team folder, or a shared folder that isn't added to your Dropbox, is `ns:<id>/...`; `list_shared_folders` and `./setup.py shared` show the ids. With team folders at the top level (`DROPBOX_TEAM_SPACE=true`), paths start at the team's root, and your own files are under `/<your name>`.
- **One folder only** (`DROPBOX_FOLDER`): clients see that folder as `""` and can't reach anything outside it. `ns:` and `id:` paths are refused, `list_shared_folders` isn't offered, and `WRITABLE_PATHS` are inside the folder, given from it (`/Outbox`) or in full (`ns:2345678901/Drawings/Outbox`). Clients see them from it. With `ns:2345678901/Drawings`, for example, `/2024/plan.pdf` is `Drawings/2024/plan.pdf` in that team folder.
- **Everything is read-only** except inside `WRITABLE_PATHS`. Matching ignores case, as Dropbox does, and paths with `..` are refused.
- **A folder has two forms.** A shared folder added to your Dropbox can be written to by its path (`/Team Docs`) only if that form is listed, and by its `ns:` form only if that form is listed.
- **A write needs both** a writable path and the caller's `dropbox:write` permission. Caddy can't tell reads from writes, because every MCP call is a `POST /mcp`, so the server checks the permission itself.
- **Big files go up in pieces.** One `write_file` call carries at most `MAX_WRITE_BYTES` (100 MiB by default, shown by `account_info`), so a file of several GB is written in a series of calls with the same path: the first with `more=true` returns an `upload_id`, each next one passes that `upload_id`, the `offset` the previous call returned and `more=true`, and the last one passes `more=false`, which saves the file. The pieces wait at Dropbox (for up to 7 days, and up to 350 GB in all), so a server restart in the middle doesn't lose them. A wrong `offset` gets Dropbox's *incorrect_offset* error, which names the right one. Uploads this size are for scripts and programs. A chat model can't type out gigabytes of base64.
- **Nothing writable** means setup links Dropbox without write access at all. Even the server's own Dropbox link can't change anything.

### Settings reference (`dropbox-mcp/.env`)

| Setting | Meaning | Default |
| --- | --- | --- |
| `APP_KEY`, `APP_SECRET`, `DROPBOX_REFRESH_TOKEN` | As in Part A | (setup) |
| `DROPBOX_FOLDER` | The only folder clients can read: `/path`, or `ns:<id>/path` in a team folder or shared folder. Empty for all of Dropbox. | all of Dropbox |
| `WRITABLE_PATHS` | Comma-separated folders or files clients may write to, inside `DROPBOX_FOLDER` if it's set (from it, or in full) | none |
| `WRITE_PERMISSION` | Auth0 permission needed to write | `dropbox:write` |
| `DROPBOX_TEAM_SPACE` | `true` to browse from a Dropbox Business team's root | `false` |
| `MAX_READ_BYTES` | Largest file `read_file` returns | `10485760` (10 MB) |
| `LIST_LIMIT` | Most entries `list_folder` returns | `2000` |
| `MAX_WRITE_BYTES` | Most bytes one `write_file` call carries; bigger files go in pieces. At most `157286400` (150 MiB), Dropbox's limit per request. The server holds a few times this in memory per call. | `104857600` (100 MiB) |
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
| `./setup.py service` says *systemd isn't running* | This machine doesn't use systemd, e.g. it's a container or WSL without systemd. Use Docker ([A2](#a2-start-it-with-docker)). |
| `./setup.py service` stops at *ensurepip is not available* | Install the package that the message names, e.g. `sudo apt install python3.12-venv`, and run `./setup.py service` again. |
| `./setup.py service` says it *can't read and write* the folder | The service runs as you, so you need access to the folder, e.g. `sudo chown -R $USER /srv/data`. Or run setup as a user who has access. |
| `./setup.py service` says the folder *is also running in Docker* | Stop the Docker one first, with `docker compose down` in that folder, so the folder isn't synced twice at once. |
| The service log says *Dropbox refused the saved settings* | Run `./setup.py service` again. It shows what's wrong and asks for new settings. |
| The log says *already running* | Something else is syncing this folder, e.g. a cron job or a `main.py` you started. Only one may run at a time. |
| The logs say *SYNC_DIRECTION must be BACKUP or RESTORE* | Fix the spelling in `.env`, or run setup again. |
| A shared or team folder's path gives *path/not_found* | If it isn't in your own folder, use its `ns:` path from `./setup.py shared`. |
| Restored files belong to root | Docker runs as root. Use `chown`, or [run it as a systemd service](#a3-or-run-it-as-a-systemd-service), which runs as you. |
| Changes to `post-download-hook.sh` have no effect | Rebuild: `docker compose up -d --build`. |
| The logs say *Failed to upload ... insufficient_space* | Your Dropbox is full. Failed files are retried every pass. |
| `docker compose up` for dropbox-mcp says *network caddy-auth0 ... could not be found* | Start caddy-auth0 first. |
| `write_file` says *is read-only* | Add the folder to the writable folders with `./setup.py mcp`, then `docker compose up -d` in `dropbox-mcp`. Check whether you listed its `/path` or `ns:` form. |
| `write_file` says *Writing needs the 'dropbox:write' permission* | Grant `dropbox:write` in Auth0, or mint a token with `-p mcp:dropbox,dropbox:write`. |
| MCP clients get `401` or `403` | See the caddy-auth0 README's troubleshooting. `./mcp-token check <token>` tests a token. |
| `write_file` gets *413 Request body too large* | The piece is over `MAX_WRITE_BYTES`. Send smaller pieces (see *Big files go up in pieces*), or raise the setting. |
| MCP clients get *Error POSTing to endpoint* | Caddy couldn't reach the server. In `dropbox-mcp`, `docker compose ps` shows whether it's running, and `docker compose logs` why not. For a few seconds while it restarts, this is normal. |
| A tool says *Dropbox said: ...* | Dropbox refused, and the rest says why. *missing_scope*: tick that permission on the app's Permissions tab if it isn't, then run `./setup.py mcp` and link Dropbox again. *no_write_permission*: the account can't write there. Dropbox only lets apps write into a shared folder that's added to your Dropbox. |

## Development

```sh
python3 -m venv venv
venv/bin/pip install -r backup-docker/requirements.txt -r dropbox-mcp/requirements.txt
PATH=$PWD/venv/bin:$PATH ./run_tests.sh
```

`run_tests.sh` runs the setup tests (`tests/`), then each part's own tests. Those can also run alone: `backup-docker/run_tests.sh`, which requires 95% coverage, and `dropbox-mcp/run_tests.sh`.

```
setup.py                  interactive setup for both parts, and the systemd service installer
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
