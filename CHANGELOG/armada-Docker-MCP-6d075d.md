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
