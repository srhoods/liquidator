# Liquidator

Web portal that records perpetual-futures liquidations from **Binance** (USD-M and COIN-M), **Bybit** (linear and
inverse) and **OKX** (swaps), shows a live tape with 1h/4h/24h/7d statistics, sends alert feeds to Discord, Telegram,
Slack or a generic webhook, and manages its own encrypted backups and restores from the UI.

It replaces the old BitMEX Discord bot (BitMEX has ceased operations).

## Architecture

Four containers in one podman **pod** (shared `localhost`, no DNS-enabled network), managed as systemd Quadlets (`deploy/quadlet/`):

| Container | Role |
|---|---|
| `liquidator-db` | PostgreSQL 16, reachable only inside the pod (**not published**), data in `/var/lib/liquidator/pgdata` |
| `liquidator-worker` | exchange collectors, batch writer, hourly rollups, feed dispatcher, backup scheduler |
| `liquidator-web` | FastAPI UI (HTMX + Alpine + Chart.js, vendored, no CDN), live updates via Postgres `LISTEN/NOTIFY` over WebSocket |
| `liquidator-proxy` | Caddy, HTTPS on 443 with its internal CA (80 redirects). CA cert at `/ca.crt` |

`worker` and `web` run the same image (`localhost/liquidator`); restarting the UI does not interrupt collection.

## Deploy

```
./deploy/deploy.sh     # rsync to /opt/liquidator on the host, podman build, install Quadlets, restart
```
`nogit/` (SSH key, old bot) is excluded from rsync, git and the image build.

### Deploying to a new host

```
HOST=root@newhost ./deploy/deploy.sh
```
`deploy/bootstrap.sh` runs on the target first and is idempotent: it installs podman (dnf), creates the data
directories under `/var/lib/liquidator`, and creates the podman secrets `liquidator_db_password`,
`liquidator_app_key` (Fernet) and `liquidator_backup_key` (age) if they don't exist. Existing ones are never touched.
Quadlets need these up front, so skipping bootstrap makes `liquidator-db` fail with `status=125`.

Moving an existing installation? A restored database and old backups are only readable with the *original* keys,
so carry them across (copy them off the old host with `podman secret inspect --showsecret`):
```
APP_KEY_FILE=app.key BACKUP_KEY_FILE=backup.key HOST=root@newhost ./deploy/deploy.sh
```
Then upload/restore a backup from the Data page. Deploy first with a fresh install, restore second: the database
password is generated per host and Postgres keeps the one it was initialised with, so never copy a `pgdata`
directory between hosts without the matching `liquidator_db_password` secret.

If a unit fails to start, `journalctl -u liquidator-db -n 30` shows podman's reason. `status=125` means podman could
not create the container: a missing secret or bind-mount directory, an unreachable image registry, or a port conflict.
**Port 53 / Pi-hole:** containers do not use a DNS-enabled podman network, so a DNS server on the host (Pi-hole, dnsmasq, ...) cannot
conflict with aardvark-dns (`failed to bind udp listener on 10.89.0.1:53`). If the host's own resolver is a loopback address,
`deploy/configure-dns.sh` finds an address of it that containers can reach and pins it; set `DNS_CANDIDATES="<ip>"` to choose one
yourself (the resolver must answer queries arriving from the podman bridge, e.g. Pi-hole set to listen on all interfaces).
Only ports 80 and 443 are published; if something else already uses them, deploy warns and the proxy will not start.

On hosts with firewalld, open the portal with `firewall-cmd --permanent --add-service={http,https} && firewall-cmd --reload`.

Create the first admin:
```
podman exec -it liquidator-web liquidator create-admin <username>
```
Other users are managed in the UI (Users).

## Using it

* **Dashboard**: tiles, charts, live tape. Red ▼ = a long was liquidated (forced sell); green ▲ = a short was liquidated.
* **History**: filters + CSV export. **Status**: collector health.
* **Feeds** (admin): per-feed filters (exchange, asset, side, minimum USD) and either *every event* (batched) or a *cascade alert*
  (total ≥ $X within N minutes, with cooldown). Secrets are encrypted at rest and never shown again. "Send test message" on each feed.
* **Data** (admin): backups, restore, schedule, remote targets (S3-compatible, SFTP with pinned host key), raw-data retention, vacuum.
* **Audit** (admin): who changed what.

## Backups and recovery

Backups are `pg_dump -Fc` archives encrypted with [age](https://age-encryption.org) to the key in the `liquidator_backup_key` secret.
**Keep a copy of that key somewhere else** (Data → Download backup key); without it backups cannot be read.

Decrypt and inspect a backup without the app:
```
age -d -i liquidator-backup.key liquidator-YYYYmmdd-HHMMSS.dump.age > backup.dump
pg_restore --list backup.dump
```
Restore (UI) verifies the file, takes a safety backup, pauses ingest, restores into a scratch database and then swaps it in by rename,
so a failed restore never touches live data. It replaces *everything*, including users and feeds. Rebuilding from nothing: start the stack,
upload the file on the Data page (or place it in `/var/lib/liquidator/backups`), and restore it.

## Notes and limits

* Binance publishes at most one liquidation per symbol per second. Bybit and OKX report all of them.
* COIN-M / inverse markets are quiet; Binance's COIN-M "all markets" stream mirrors USD-M, so COIN-M uses per-symbol subscriptions.
* Binance USD-M uses the `/market/ws` endpoint; the legacy `/ws` path no longer delivers `forceOrder`.
* Asset names are as the exchange reports them (e.g. `1000PEPE`).
* Tests: `podman run --rm localhost/liquidator pytest` (parsers, notifier, backup crypto).
