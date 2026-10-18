# Backup Docker
Backup Docker System

This project is a docker (or a standalone installation) and checks a specified folder (mounted volume in docker) for changes, then uploads those to a cloud provider (such as Dropbox or S3).

## Features
- Incremental uploads only.  Takes checksums and then updates a `checksum.txt` file
- Logs errors to syslog (or rsyslog)
- Intended to run as a CRON

## Dropbox and OAuth2
1.  Run `obtain_refresh_token.py`.  This should result in a URL
2.  Open that URL from a browser.  Click through it and it should redirect you to a URL that end with `?code=XXXX`
3.  Copy that `XXXX` value
4.  Run `obtain_refresh_token2.py`.  Paste the `XXXX` value there
5.  Copy the Refresh token and paste it into `.env`

# Dropbox MCP
`dropbox-mcp/` is an MCP server (Streamable HTTP at `/mcp`, port 8000) that gives MCP clients such as LibreChat access to a Dropbox account, including folders shared with it. Everything is read-only except the folders listed in `WRITABLE_PATHS`.

It has no authentication of its own. It runs behind [caddy-auth0](https://github.com/amunchet/caddy-auth0), which checks the caller's token and permissions and passes them on in `X-Auth-*` headers.

## Tools
| Tool | What it does |
| --- | --- |
| `account_info` | The Dropbox account, the caller, and the writable paths |
| `list_folder` | A folder's contents, optionally recursive (capped at `LIST_LIMIT`) |
| `search` | Search names and contents, optionally under a path |
| `get_metadata` | Details of one file or folder |
| `read_file` | Text, or the image for images, or base64 for anything else (capped at `MAX_READ_BYTES`) |
| `list_shared_folders` | Every shared folder the account can reach, including ones not added to its Dropbox |
| `write_file` | Create (or with `overwrite`, replace) a file inside a writable path. The only tool that writes |

Paths look like `/Folder/file.txt`, and `""` is the root. A shared folder that isn't added to the account's Dropbox is reached as `ns:<shared_folder_id>/...`, which `list_shared_folders` shows.

## Writes
A write is allowed only when both hold:
1. The path is inside one of `WRITABLE_PATHS` (comma-separated, case-insensitive, e.g. `/Shared/Team,ns:1234567`). Paths with `..` are refused, as are `id:` paths. A folder listed by its path isn't writable through its `ns:` form, or the other way round: list the form clients use.
2. The caller has `WRITE_PERMISSION` (default `dropbox:write`) in Auth0. Caddy can't tell reads from writes here, since every MCP call is a `POST /mcp`, so the server checks this itself.

If nothing should ever be writable, leave `WRITABLE_PATHS` empty and leave `files.content.write` off the Dropbox app, so the token itself can't write.

## Setup
1. **Dropbox app.** At https://www.dropbox.com/developers/apps create an app with **Full Dropbox** access ("App folder" apps can't see shared folders). Under *Permissions* enable `account_info.read`, `files.metadata.read`, `files.content.read`, `sharing.read`, and `files.content.write` only if you'll use `WRITABLE_PATHS`.
2. **Refresh token.** Use `backup-docker/obtain_refresh_token.py` and `obtain_refresh_token2.py` as described above. Scopes are fixed when the token is issued, so get a new token after changing them.
3. **Configure.** Copy `dropbox-mcp/.env.sample` to `dropbox-mcp/.env` and fill it in. For Dropbox Business team folders, set `DROPBOX_TEAM_SPACE=true`; paths (including `WRITABLE_PATHS`) are then relative to the team root, and your own folder is under `/<your name>`.
4. **Run.** With caddy-auth0 running, `cd dropbox-mcp && docker compose up -d --build`. It joins the `caddy-auth0` network and publishes no ports.
5. **Caddy.** Add a site to caddy-auth0's `Caddyfile`, then reload Caddy:
   ```caddyfile
   dropbox-mcp.example.com {
   	auth0 {
   		mcp
   		require mcp:dropbox
   	}
   	reverse_proxy dropbox-mcp:8000
   }
   ```
   Grant `mcp:dropbox` (and `dropbox:write` to those who may write) in Auth0, or mint a local token with `./mcp-token dropbox-mcp librechat`, adding `-p mcp:dropbox,dropbox:write` for write access.

Every caller sees the same Dropbox account: Auth0 decides who may use the server, not whose Dropbox it opens.

## Tests
`cd dropbox-mcp && pip install -r requirements.txt && ./run_tests.sh`
