#!/usr/bin/env bash
# Sync the working tree to a host, prepare it, build the image and (re)start the stack.
#
#   ./deploy/deploy.sh                      # deploy to rocky98-1.lab.local
#   HOST=root@other.example ./deploy/deploy.sh
#   SSH_KEY=path/to/key  APP_KEY_FILE=... BACKUP_KEY_FILE=... ./deploy/deploy.sh
#   DNS_CANDIDATES="192.168.1.2" ./deploy/deploy.sh   # resolver address containers should use, if auto-detection is wrong
#
# APP_KEY_FILE / BACKUP_KEY_FILE are only for moving an existing installation: they carry over the
# Fernet key and age backup key so a restored database and old backups stay readable on the new host.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${HOST:-root@rocky98-1.lab.local}
SSH_KEY=${SSH_KEY:-nogit/id_ed25519}
# use the lab key when present, otherwise fall back to the user's default SSH keys/agent
if [[ -f $SSH_KEY ]]; then SSH="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes"; else SSH="ssh -o BatchMode=yes"; fi

# rsync must exist on the target before we can sync to it
$SSH "$HOST" 'command -v rsync >/dev/null || dnf install -y rsync >/dev/null'

rsync -az --delete -e "$SSH" \
  --exclude nogit/ --exclude .git/ --exclude __pycache__/ --exclude '*.pyc' \
  ./ "$HOST:/opt/liquidator/"

for pair in "APP_KEY_FILE:app_key" "BACKUP_KEY_FILE:backup_key"; do
  var=${pair%%:*}; name=${pair##*:}; file=${!var:-}
  if [[ -n $file ]]; then
    $SSH "$HOST" 'umask 077; mkdir -p /root/.liquidator-import'
    $SSH "$HOST" "umask 077; cat > /root/.liquidator-import/$name" < "$file"
  fi
done

$SSH "$HOST" env DNS_CANDIDATES="${DNS_CANDIDATES:-}" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/liquidator
bash deploy/bootstrap.sh
podman build -q -t localhost/liquidator:latest -f Containerfile .
bash deploy/configure-dns.sh

# Earlier versions used a DNS-enabled podman network; remove it (and stale unit files) before installing the pod.
if [[ -e /etc/containers/systemd/liquidator.network ]]; then
  systemctl stop liquidator-proxy liquidator-web liquidator-worker liquidator-db liquidator-network 2>/dev/null || true
  rm -f /etc/containers/systemd/liquidator.network
  systemctl daemon-reload
  podman network rm -f liquidator >/dev/null 2>&1 || true
fi
find /etc/containers/systemd -maxdepth 1 -type f -name 'liquidator*' -delete
cp deploy/quadlet/* /etc/containers/systemd/
systemctl daemon-reload
systemctl restart liquidator-pod.service
for u in liquidator-db liquidator-worker liquidator-web liquidator-proxy; do
  if ! systemctl restart "$u.service"; then
    echo "!! $u failed to start; last log lines:" >&2
    journalctl -u "$u.service" -n 15 --no-pager >&2
    exit 1
  fi
done
systemctl --no-pager --lines=0 status 'liquidator-*' | grep -E '^(●|Active)' || true
REMOTE
