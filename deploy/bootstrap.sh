#!/usr/bin/env bash
# Idempotent host preparation, run on the target host (as root) by deploy.sh.
# Installs podman, creates the data directories and the podman secrets the Quadlets need.
# Safe to re-run: anything that already exists is left alone.
set -euo pipefail

IMPORT=/root/.liquidator-import   # optional app_key / backup_key uploaded by deploy.sh
say() { printf '[bootstrap] %s\n' "$*"; }

# --- packages -----------------------------------------------------------------------------
missing=()
command -v podman      >/dev/null || missing+=(podman)
command -v python3     >/dev/null || missing+=(python3)
if ((${#missing[@]})); then
  command -v dnf >/dev/null || { echo "Need ${missing[*]} but dnf is not available; install them and re-run." >&2; exit 1; }
  say "installing: ${missing[*]}"
  dnf install -y "${missing[@]}" >/dev/null
fi

# --- directories (a missing bind-mount source makes podman exit 125) --------------------------
install -d -m 755 /var/lib/liquidator /var/lib/liquidator/backups /var/lib/liquidator/caddy /etc/containers/systemd
install -d -m 700 /var/lib/liquidator/pgdata
install -d -m 700 "$IMPORT"

# --- secrets (a missing secret makes podman exit 125) -----------------------------------------
create_secret() {  # name, value-producing command...
  local name=$1; shift
  if podman secret exists "$name"; then
    say "secret $name: already present"
  else
    "$@" | podman secret create "$name" - >/dev/null
    say "secret $name: created"
    CREATED+=("$name")
  fi
}
CREATED=()

gen_db_password() { head -c 32 /dev/urandom | base64 | tr -d '\n=+/' | head -c 40; }
gen_app_key()     { python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end="")'; }
# An age X25519 identity is a bech32 encoding of 32 random bytes (hrp "age-secret-key-"); generating it
# here avoids needing the `age` package, which is only in EPEL on Rocky/RHEL.
gen_backup_key() {
  python3 - <<'PY'
import os
CH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
def polymod(v):
    g = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    c = 1
    for x in v:
        b = c >> 25
        c = (c & 0x1ffffff) << 5 ^ x
        for i in range(5):
            c ^= g[i] if (b >> i) & 1 else 0
    return c
def convert(data):
    acc = bits = 0
    out = []
    for b in data:
        acc = acc << 8 | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(acc >> bits & 31)
    if bits:
        out.append(acc << (5 - bits) & 31)
    return out
hrp = "age-secret-key-"
d = convert(os.urandom(32))
exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
mod = polymod(exp + d + [0] * 6) ^ 1
chk = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
print(("%s1%s" % (hrp, "".join(CH[x] for x in d + chk))).upper(), end="")
PY
}

# imported keys (when moving an existing installation) take priority over generating new ones
use_import() {  # file, generator-fallback
  if [[ -s "$IMPORT/$1" ]]; then tr -d '\n' < "$IMPORT/$1"; else "$2"; fi
}
imported_app=0; imported_backup=0
[[ -s "$IMPORT/app_key" ]] && imported_app=1
[[ -s "$IMPORT/backup_key" ]] && imported_backup=1
if ((imported_app)); then
  python3 - "$IMPORT/app_key" 2>/dev/null <<'PY' || { echo "imported app_key is not a valid Fernet key" >&2; exit 1; }
import base64, sys
k = open(sys.argv[1]).read().strip()
assert len(base64.urlsafe_b64decode(k)) == 32
PY
fi
if [[ -s "$IMPORT/backup_key" ]] && ! grep -q '^AGE-SECRET-KEY-' "$IMPORT/backup_key"; then
  echo "imported backup_key is not an age secret key" >&2; exit 1
fi

create_secret liquidator_db_password gen_db_password
create_secret liquidator_app_key     use_import app_key    gen_app_key
create_secret liquidator_backup_key  use_import backup_key gen_backup_key
shred -u "$IMPORT"/* 2>/dev/null || true

# --- advisories (nothing is changed automatically) --------------------------------------------
if [[ " ${CREATED[*]:-} " == *" liquidator_db_password "* && -n "$(ls -A /var/lib/liquidator/pgdata 2>/dev/null)" ]]; then
  say "WARNING: pgdata is not empty but the DB password secret was just generated. Postgres keeps the"
  say "         password it was initialised with, so liquidator-db will reject logins. Either restore the"
  say "         original secret or move /var/lib/liquidator/pgdata aside and restore from a backup."
fi
if [[ " ${CREATED[*]:-} " == *" liquidator_backup_key "* ]] && ((!imported_backup)); then
  say "NOTE: a NEW backup key was generated. Backups made on other hosts can only be restored here if"
  say "      you deployed with BACKUP_KEY_FILE=<their key>. Download this host's key from Data -> Backups."
fi
if [[ " ${CREATED[*]:-} " == *" liquidator_app_key "* ]] && ((!imported_app)); then
  say "NOTE: a NEW app key was generated. Feed and backup-target secrets inside a restored database were"
  say "      encrypted with the old key; deploy with APP_KEY_FILE=<old key> to keep them readable."
fi
for p in 80 443; do
  if ss -tln "( sport = :$p )" 2>/dev/null | grep -q LISTEN && ! podman ps --format '{{.Names}}' | grep -q '^liquidator-proxy$'; then
    say "WARNING: something is already listening on port $p; liquidator-proxy will not be able to start."
  fi
done
if systemctl is-active --quiet firewalld 2>/dev/null; then
  say "firewalld is active: open the portal with  firewall-cmd --permanent --add-service={http,https} && firewall-cmd --reload"
fi
say "host ready"
