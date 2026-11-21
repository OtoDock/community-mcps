"""``close_exit.sh``: the rules a closed local network gets, run with every
command it calls faked in its PATH (no real firewall is touched), and its
proof: IPv6 that cannot be closed and a gateway the rules do not refuse stop
the start."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "close_exit.sh"
ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
         "eth0\t00000000\t0100C80A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n")
LO_ONLY = "00000000000000000000000000000001 01 80 10 80       lo\n"
WITH_ETH = LO_ONLY + "fe80000000000000004200fffe0a0002 02 40 20 80     eth0\n"


def _fake_bin(tmp_path: Path, *, failing_check: str = "", ip6_lists: bool = True,
              probe_ok: bool = True) -> Path:
    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("iptables", "ip6tables"):
        fail = (f'case "$*" in *"-C OUTPUT -d {failing_check} "*) exit 1;; esac\n'
                if failing_check else "")
        if tool == "ip6tables" and not ip6_lists:
            fail += 'case "$*" in *"-L OUTPUT"*) exit 1;; esac\n'
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n{fail}exit 0\n')
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "python3").write_text(
        f'#!/bin/sh\ncat > /dev/null\necho "python3 $*" >> "{log}"\nexit {0 if probe_ok else 1}\n')
    (bin_dir / "python3").chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *, if_inet6: str = LO_ONLY, **kw) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = _fake_bin(tmp_path, **kw)
    (tmp_path / "if_inet6").write_text(if_inet6)
    (tmp_path / "route").write_text(ROUTE)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin",
           "CLOSE_EXIT_IF_INET6": str(tmp_path / "if_inet6"),
           "CLOSE_EXIT_ROUTE": str(tmp_path / "route")}
    done = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True,  # noqa: S603
                          timeout=30, check=False)
    log = tmp_path / "calls.log"
    return done, (log.read_text().splitlines() if log.exists() else [])


def test_every_private_range_is_rejected_after_loopback_replies_and_dns(tmp_path):
    done, calls = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    v4 = [c for c in calls if c.startswith("iptables ")]
    assert v4[:4] == [
        "iptables -w -A OUTPUT -o lo -j ACCEPT",
        "iptables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "iptables -w -A OUTPUT -p udp --dport 53 -j ACCEPT",
        "iptables -w -A OUTPUT -p tcp --dport 53 -j ACCEPT",
    ]
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"):
        rule = f"OUTPUT -d {net} -j REJECT --reject-with icmp-admin-prohibited"
        assert f"iptables -w -A {rule}" in v4
        assert f"iptables -w -C {rule}" in v4
    assert not [c for c in calls if c.startswith("ip6tables ")], "IPv6 only on the loopback"
    assert calls[-1] == f"python3 - {tmp_path / 'route'}", "the gateway probe runs last"


def test_a_rule_that_did_not_hold_stops_the_start(tmp_path):
    done, _calls = _run(tmp_path, failing_check="192.168.0.0/16")
    assert done.returncode != 0


def test_ipv6_beside_the_loopback_is_closed_too(tmp_path):
    done, calls = _run(tmp_path, if_inet6=WITH_ETH)
    assert done.returncode == 0, done.stderr
    assert "ip6tables -w -A OUTPUT -d fc00::/7 -j REJECT --reject-with icmp6-adm-prohibited" in calls
    assert "ip6tables -w -C OUTPUT -d fe80::/10 -j REJECT --reject-with icmp6-adm-prohibited" in calls


def test_ipv6_that_cannot_be_closed_stops_the_start(tmp_path):
    done, calls = _run(tmp_path, if_inet6=WITH_ETH, ip6_lists=False)
    assert done.returncode != 0
    assert "IPv6 is up" in done.stderr
    assert not [c for c in calls if c.startswith("python3")]
    assert "closed" not in done.stdout


def test_a_gateway_the_rules_do_not_refuse_stops_the_start(tmp_path):
    done, _calls = _run(tmp_path, probe_ok=False)
    assert done.returncode != 0
    assert "closed" not in done.stdout
