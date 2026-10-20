# Changelog: Docker MCP

- Armada session: `Docker MCP`
- Branch: `armada/Docker-MCP-6d075d`
- Base branch: `master`
- Started: 2026-10-18 07:53 UTC

Entries are appended newest last, each stamped with the UTC date and time.

## 2026-10-18 03:05 CDT
Added `dropbox-mcp/`, an MCP server for Dropbox meant to sit behind caddy-auth0. It reads the whole account, including shared folders not added to it (via `ns:<id>` paths) and, optionally, Dropbox Business team folders. Everything is read-only except `write_file`, which only writes inside `WRITABLE_PATHS` and only for callers holding `WRITE_PERMISSION` (checked in the server, since Caddy can't tell reads from writes on `POST /mcp`). Includes Dockerfile, compose file joining the `caddy-auth0` network, tests, and a README section.

## 2026-10-19 16:50 CDT
Rewrote restore (`backup-docker/restore.py`). It now copies the whole Dropbox folder, subfolders included, with the same structure; before, it only fetched top-level files and saved them all under their bare names. It works with shared folders, including ones not added to the account's Dropbox (`ns:<id>` paths). It compares Dropbox's content hash instead of downloading every file on every pass, and remembers what it downloaded in `.restore-state.json`, so files edited by the post-download hook are no longer re-downloaded and re-hooked each pass. Downloads are atomic and honor `ignore.txt`.

## 2026-10-19 16:50 CDT
Consolidated backup-docker around a shared `common.py` (settings, logging, Dropbox client, ignore patterns) and fixed bugs found on the way. Backup no longer refuses to start without the undocumented `DROPBOX_TOKEN`. Failed uploads are retried instead of being recorded as done. A misspelled `SYNC_DIRECTION` stops with an error instead of silently restoring. A failed pass is logged and retried instead of crashing the container. Logging to `/var/log` is now opt-in (`LOG_FILE`), so non-root runs don't crash. Time travel uses the Dropbox SDK like the rest, and `--list N` now shows N revisions instead of one. `main.py --once` runs a single pass. Docker: `.env` is no longer baked into the image (passed with `env_file`), the image is Python 3.13, and the stale `temp` mount and fixed container name are gone. The stale backup tests are fixed, and new ones cover restore, time travel and the main loop.

## 2026-10-19 16:50 CDT
Added an interactive `./setup.py` that replaces the broken setuptools stub and the two `obtain_refresh_token` scripts. It needs only Python 3. It links Dropbox in one step with no redirect URI, asking only for the permissions each part needs (the MCP server gets write access only when folders are writable). It writes each part's `.env`, checks that the Dropbox and local folders exist, lists shared folders with the `ns:` paths to use for them, and prints the Caddy, Auth0 and LibreChat steps for the MCP server. `./setup.py check` re-tests saved settings.

## 2026-10-19 16:50 CDT
Rewrote the README as one step-by-step guide: creating the Dropbox app (access type and the five permissions), backup and restore setup, how folders map into Docker, shared folders, the post-download hook, time travel, several folders, running under systemd or cron, the MCP server behind caddy-auth0, a settings reference and troubleshooting. Removed the placeholder `systemd/` folder; the README has a working unit instead. Added a root `run_tests.sh` for all suites.

## 2026-10-19 16:50 CDT
The MCP server now always requires `WRITE_PERMISSION` for writes. An empty value means the default `dropbox:write`, instead of silently turning the check off.

## 2026-10-19 19:13 CDT
backup-docker now stops cleanly and can't get stuck after a crash. The `pid` package's lock is replaced by a plain `flock` that the kernel releases when the process dies. Before, after a power cut, a leftover PID file whose number now belonged to another process kept every later start failing with "already running". `pid` also exited with status 1 on SIGTERM, which made systemd mark each `systemctl stop` as a failure. SIGTERM now exits cleanly and saves the pass's progress. In Docker, that cuts `docker stop` from a 10-second timeout and SIGKILL to about a second. Restore writes `.restore-state.json` atomically, so being stopped mid-write can't corrupt it. The lock now applies in Docker too, so `docker compose exec ... main.py --once` can't overlap the running loop.

## 2026-10-19 19:13 CDT
Added `./setup.py service`, a guided installer that runs backup-docker as a systemd service instead of in Docker, and `./setup.py remove-service` to undo it.
- **Checks first:** it confirms systemd, sudo and Python 3.8+ are available, and refuses if the same folder is also running in Docker.
- **Sets it up:** it builds a venv, rebuilding it if the OS upgrade broke it. It keeps working settings or asks the setup questions, and checks that the service user can read and write the folder. Then it installs the packages and writes, enables and starts a unit.
- **Confirms it:** it waits for the service's first log line and shows it, or shows the log of a service that failed to start and why.
- **Who it runs as:** the service runs as the installing user, so restored files belong to them. Running the installer under sudo asks before installing a root service.
- **Copies:** each copy of backup-docker gets its own service, e.g. `dropbox-backup-photos`. Re-running the installer updates settings, packages and the unit, then restarts the service.
- **Testing:** verified end to end on real systemd 245 (Ubuntu 20.04, Python 3.8) and 255 (Ubuntu 24.04, Python 3.12). That covered a path containing spaces, a reboot, SIGKILL recovery and a missing `python3-venv`.
- **README:** a walkthrough section, a combined Docker and systemd everyday-commands table, and troubleshooting rows. It also explains why dropbox-mcp stays Docker-only: outside Docker, its port would bypass Auth0.

## 2026-10-19 21:10 CDT
Fixed `ns:<id>` folders: team folders, and shared folders not added to the account, are now reachable. Before, every `ns:` path got *path/not_found*, so they could be neither restored nor chosen in setup (reported with a Dropbox Business team folder). Dropbox only reaches what is inside the folder a request is rooted at, which is the user's own folder by default, so an `ns:` path given as a plain path can't reach anything outside it. Requests for an `ns:<id>/...` path now set `Dropbox-API-Path-Root` to that namespace (`namespace_id` mode) and use the rest as the path. This applies to setup's folder checks, backup and restore passes (rooted once at startup and handed the folder), `time_travel.py`, and every dropbox-mcp tool, which also maps returned paths back to the `ns:` form. `./setup.py shared` now labels team folders as such. The test fakes model Dropbox's rooting, so the old behavior fails them. The README states that apps can write into a shared folder only once it is added to the account.

## 2026-10-20 17:49 CDT
Explained why dropbox-mcp's container has no volumes, after being asked how it can reach Dropbox without one. It keeps no files: each tool call is a live request to the Dropbox API, made with the app key, secret and refresh token in `.env`. The explanation is in the README's Part B and in `dropbox-mcp/docker-compose.yml`.
