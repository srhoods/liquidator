#!/usr/bin/env bash
# Run on the target host after the image is built. Makes sure containers can resolve exchange hostnames.
#
# Containers in a pod use the default podman network, which copies the host's /etc/resolv.conf. When the host
# resolver is a loopback address (a local Pi-hole/dnsmasq/unbound), that address means "the container itself",
# so lookups fail or podman silently falls back to public DNS. In that case find an address of the host's
# resolver that containers can reach, and pin it with a drop-in. Probes use the real image.
set -euo pipefail
IMG=localhost/liquidator:latest
DROPDIR=/etc/containers/systemd/liquidator.pod.d
DROPIN=$DROPDIR/10-dns.conf
PROBE=${DNS_PROBE_HOST:-api.bybit.com}
say() { printf '[dns] %s\n' "$*"; }

resolves() {  # [dns-server]
  timeout 30 podman run --rm --pull=never ${1:+--dns "$1"} "$IMG" \
    python -c "import socket; socket.gethostbyname('$PROBE')" >/dev/null 2>&1
}

first_ns=$(awk '/^nameserver/{print $2; exit}' "${RESOLV_CONF:-/etc/resolv.conf}")
if [[ -n $first_ns && $first_ns != 127.* && $first_ns != ::1 ]] && resolves; then
  rm -f "$DROPIN"
  say "containers use the host's resolver ($first_ns)"
  exit 0
fi

# DNS_CANDIDATES="ip ip ..." overrides auto-detection (e.g. DNS_CANDIDATES=192.168.1.2 ./deploy/deploy.sh)
if [[ -n ${DNS_CANDIDATES:-} ]]; then
  candidates=$DNS_CANDIDATES
else
  lan_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')
  bridge_gw=$(podman network inspect podman --format '{{(index .Subnets 0).Gateway}}' 2>/dev/null || true)
  candidates="$lan_ip $bridge_gw"
fi
for cand in $candidates; do
  [[ -n $cand ]] || continue
  if resolves "$cand"; then
    install -d "$DROPDIR"
    printf '[Pod]\nDNS=%s\n' "$cand" > "$DROPIN"
    say "host resolver is loopback ($first_ns); containers will query it via $cand"
    exit 0
  fi
  say "$cand did not answer DNS queries from a container"
done

rm -f "$DROPIN"
say "WARNING: no reachable resolver found for containers (host resolver: ${first_ns:-none})."
say "         Podman will fall back to public DNS, or lookups of exchange hostnames may fail."
say "         Fix the resolver's listen/ACL settings, or create $DROPIN containing:  [Pod]  DNS=<ip>"
