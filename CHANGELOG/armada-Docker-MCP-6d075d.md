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
