#!/usr/bin/env bash
# Sync the working tree to the lab host, build the image and (re)start the stack.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${HOST:-root@rocky98-1.lab.local}
SSH="ssh -i nogit/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes"

rsync -az --delete -e "$SSH" \
  --exclude nogit/ --exclude .git/ --exclude __pycache__/ --exclude '*.pyc' \
  ./ "$HOST:/opt/liquidator/"

$SSH "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/liquidator
podman build -q -t localhost/liquidator:latest -f Containerfile .
cp deploy/quadlet/* /etc/containers/systemd/
systemctl daemon-reload
for u in liquidator-db liquidator-worker liquidator-web liquidator-proxy; do
  systemctl restart "$u.service"
done
systemctl --no-pager --lines=0 status 'liquidator-*' | grep -E '^(●|Active)' || true
REMOTE
