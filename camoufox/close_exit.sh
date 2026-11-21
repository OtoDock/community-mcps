#!/bin/sh
# Close this container's own local network (OTODOCK_LOCAL_NETWORK=closed).
#
# Run by the entrypoint while it is still root, with the one capability the
# platform adds for it (NET_ADMIN); the entrypoint drops that capability from
# the bounding set before the browser starts, so nothing the browser runs can
# take the rules back. Every NEW connection to a private, shared, link-local,
# benchmark, multicast or reserved address is rejected, the container's own
# Docker network, its sibling containers and its gateway included. Kept: the
# loopback, the replies to connections made to this container (the platform
# reaches it inbound), and DNS (Docker's resolver forwards from this
# namespace, to a resolver that may sit on the LAN). Exits non-zero when a
# rule did not hold, when IPv6 is up and cannot be closed, or when a new
# connection to the gateway is not refused by the rules: the entrypoint then
# stops rather than run open.
#
# Two limits: a LAN with global IPv6 addresses is outside the ranges below,
# so on an IPv6-enabled Docker network those hosts stay reachable by their
# global address; and port 53 is accepted to every destination, so code the
# browser's server runs can open TCP or UDP 53 on a private host (a page
# cannot: 53 is a browser bad port).
set -eu

V4="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 0.0.0.0/8 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4"
V6="fc00::/7 fe80::/10 ff00::/8"
IF_INET6="${CLOSE_EXIT_IF_INET6:-/proc/net/if_inet6}"
ROUTE="${CLOSE_EXIT_ROUTE:-/proc/net/route}"

close() { # close <iptables binary> <reject type> <networks>
    tool="$1"
    reject="$2"
    shift 2
    "$tool" -w -A OUTPUT -o lo -j ACCEPT
    "$tool" -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    "$tool" -w -A OUTPUT -p udp --dport 53 -j ACCEPT
    "$tool" -w -A OUTPUT -p tcp --dport 53 -j ACCEPT
    for net in "$@"; do
        "$tool" -w -A OUTPUT -d "$net" -j REJECT --reject-with "$reject"
    done
    for net in "$@"; do
        "$tool" -w -C OUTPUT -d "$net" -j REJECT --reject-with "$reject"
    done
}

# An administrative rejection, not a port one: the probe below tells it from
# a host that simply answers "closed".
# shellcheck disable=SC2086  # the lists split on purpose
close iptables icmp-admin-prohibited $V4

# IPv6 on any interface but the loopback must be closed too, or the start
# stops: a container that holds an IPv6 address is never left open on it.
v6=""
if [ -r "$IF_INET6" ]; then
    while read -r _addr _index _len _scope _flags dev; do
        [ "$dev" = "lo" ] || v6=1
    done < "$IF_INET6"
fi
if [ -n "$v6" ]; then
    ip6tables -w -L OUTPUT >/dev/null 2>&1 || {
        echo "camoufox: IPv6 is up and ip6tables cannot close it" >&2
        exit 1
    }
    # shellcheck disable=SC2086
    close ip6tables icmp6-adm-prohibited $V6
fi

# The rules, proven: a new connection to the default gateway must be refused
# by them (an administrative rejection), not answered by the host.
python3 - "$ROUTE" <<'PROBE'
import errno, socket, sys

gateway = ""
try:
    with open(sys.argv[1]) as table:
        for line in table.read().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                gateway = socket.inet_ntoa(int(parts[2], 16).to_bytes(4, "little"))
                break
except OSError:
    pass
if not gateway:
    print("camoufox: no default route, nothing to probe")
    sys.exit(0)
probe = socket.socket()
probe.settimeout(3)
try:
    probe.connect((gateway, 9))
except OSError as e:
    if e.errno in (errno.EHOSTUNREACH, errno.EACCES, errno.EPERM):
        sys.exit(0)
    sys.stderr.write(f"camoufox: the gateway {gateway} answered past the rules ({e})\n")
    sys.exit(1)
sys.stderr.write(f"camoufox: a new connection to the gateway {gateway} went through\n")
sys.exit(1)
PROBE
echo "camoufox: the local network is closed"
