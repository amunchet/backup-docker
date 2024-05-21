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
