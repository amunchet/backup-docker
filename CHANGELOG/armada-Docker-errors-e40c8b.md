# Changelog: Docker errors

- Armada session: `Docker errors`
- Branch: `armada/Docker-errors-e40c8b`
- Base branch: `master`
- Started: 2026-11-26 09:36 UTC

Entries are appended newest last, each stamped with the UTC date and time.

## 2026-11-26 04:02 CST
backup-docker now rides out network and DNS outages instead of restarting, and still restarts when something stays wrong.
- **What Graylog showed:** about 5,000 restarts of the ten dropbox-backup containers in 30 days, up to about 2,000 on one host. About 97% were the Dropbox token refresh failing because DNS or the network was down (*Failed to resolve 'api.dropboxapi.com'*, *Network is unreachable*). The rest were Dropbox 500/503s, timeouts and connection resets. They came in bursts: each restart crashed again within seconds, e.g. about 2,700 restarts during one 10-hour outage on 2026-11-23. The hosts run 2025 builds whose loop let any error end the process.
- **Master before this change** survived failures mid-run, but still crashed at startup while the network or DNS was down, e.g. after a reboot. A Dropbox 5xx at startup exited as "Dropbox refused the saved settings". It also never restarted, however long its passes failed.
- **Now:** each pass reaches Dropbox first, at startup too. A failed pass logs one line, with a traceback only for errors that aren't outages, and the next pass tries again. After an hour of failed passes, `main.py` exits, so Docker or systemd restarts it and the restart shows the trouble. The check also lets a backup pass with nothing to upload notice an outage, which it never did. Settings Dropbox refuses (`AuthError`, `BadInputError`) still stop it at once, now also mid-run. `--once` still fails straight away.
- **Testing:** unit tests for each path, each checked to fail with its behavior removed. In Docker with no network, the new image ran 40 s with 0 restarts while the deployed-era image restarted 9 times. With the limit cut to 8 s it exited and was restarted once, and fake credentials stopped it at once with the setup hint.
