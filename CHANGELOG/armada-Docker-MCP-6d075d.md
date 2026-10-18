# Changelog: Docker MCP

- Armada session: `Docker MCP`
- Branch: `armada/Docker-MCP-6d075d`
- Base branch: `master`
- Started: 2026-10-18 07:53 UTC

Entries are appended newest last, each stamped with the UTC date and time.

## 2026-10-18 03:05 CDT
Added `dropbox-mcp/`, an MCP server for Dropbox meant to sit behind caddy-auth0. It reads the whole account, including shared folders not added to it (via `ns:<id>` paths) and, optionally, Dropbox Business team folders. Everything is read-only except `write_file`, which only writes inside `WRITABLE_PATHS` and only for callers holding `WRITE_PERMISSION` (checked in the server, since Caddy can't tell reads from writes on `POST /mcp`). Includes Dockerfile, compose file joining the `caddy-auth0` network, tests, and a README section.
