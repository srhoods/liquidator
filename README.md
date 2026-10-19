# Liquidator

Web portal that records perpetual-futures liquidations from **Binance** (USD-M and COIN-M), **Bybit** (linear and
inverse) and **OKX** (swaps), shows a live tape with 1h/4h/24h/7d statistics, sends alert feeds to Discord, Telegram,
Slack or a generic webhook, and manages its own encrypted backups and restores from the UI.

It replaces the old BitMEX Discord bot (BitMEX has ceased operations).

## Architecture

Four podman containers on an internal network, managed as systemd Quadlets (`deploy/quadlet/`):

| Container | Role |
|---|---|
| `liquidator-db` | PostgreSQL 16, **no published port**, data in `/var/lib/liquidator/pgdata` |
| `liquidator-worker` | exchange collectors, batch writer, hourly rollups, feed dispatcher, backup scheduler |
| `liquidator-web` | FastAPI UI (HTMX + Alpine + Chart.js, vendored, no CDN), live updates via Postgres `LISTEN/NOTIFY` over WebSocket |
| `liquidator-proxy` | Caddy, HTTPS on 443 with its internal CA (80 redirects). CA cert at `/ca.crt` |

`worker` and `web` run the same image (`localhost/liquidator`); restarting the UI does not interrupt collection.

## Deploy

```
./deploy/deploy.sh     # rsync to /opt/liquidator on the host, podman build, install Quadlets, restart
```
`nogit/` (SSH key, old bot) is excluded from rsync, git and the image build.

One-time host setup (already done on `rocky98-1.lab.local`): install `podman`, create `/var/lib/liquidator/{pgdata,backups,caddy}`
and the podman secrets `liquidator_db_password`, `liquidator_app_key` (Fernet key) and `liquidator_backup_key` (age identity).

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
