"""«Exit-node egress-мост» — the COMPLEMENT of mobile_proxy.py. Where mobile_proxy consumes phones
that run a SOCKS server themselves (Android), this turns a phone that is a Tailscale EXIT NODE
(works on iPhone AND Android — an iPhone cannot run a SOCKS server, so exit-node is its only egress
path) into a LOCAL no-auth SOCKS5 endpoint on 127.0.0.1 that the residential proxy pool round-robins.

Mechanism (flags verified on tailscale 1.102.3): for each phone slot we run a dedicated USERSPACE
`tailscaled` (its own socket + state dir, no root needed in userspace mode — it creates no TUN),
exposing a SOCKS5 server bound to 127.0.0.1 ONLY, authed to the tailnet with a REUSABLE auth key and
PINNED to that phone's exit node. Traffic into that local SOCKS egresses via the phone's carrier/WiFi
IP. Multiple phones run concurrently WITHOUT hijacking the server's global routing (no `tailscale up
--exit-node` on the host itself). Per-slot bring-up:

    tailscaled -tun=userspace-networking -socks5-server=127.0.0.1:<port> \
      -socket=<sd>/tailscaled.sock -state=<sd>/tailscaled.state -statedir=<sd>   # background
    tailscale --socket=<sd>/tailscaled.sock up --authkey=<KEY> --hostname=jf-egress-<slot> \
      --exit-node=<phone_ip> --exit-node-allow-lan-access --accept-routes=false --ssh=false

Then `socks5://127.0.0.1:<port>` egresses via that phone.

How it plugs in: `proxy_pool.residential_slots()` appends `live_socks()` (every slot answering TCP
now) to the loopback slots, so everything that PREFERS residential (`next_proxy()` → the campaign
cron's `_do_fill`, single fills, the bulk lane) round-robins across the phones and falls back the
moment none answer. `live_socks`/`running_slots` are the hot path — TCP-only, cached, never raise.

State (backend/data/ts_egress.json, gitignored)::

    {"enabled": true, "base_port": 10800, "cursor": 0,
     "slots": {"0": {"port": 10800, "exit_ip": "100.x.y.z", "hostname": "jf-egress-0", "note": "iPhone Dana"}}}

Per-slot state dirs live under backend/data/ts-egress/<slot>/ (gitignored — they hold node keys). The
REUSABLE auth key is owner-supplied and NEVER written to a tracked file or logged (masked everywhere).

Owner go-live: (1) on the phone's Tailscale app toggle «Use as exit node» + APPROVE it in the admin
console; (2) mint a REUSABLE (ideally ephemeral+tagged) auth key in the console; (3) on the server
`PYTHONPATH=. python3 -m backend.tools.tailscale_egress --sync --authkey file:/path/key` then `--on`.

CLI:  --peers                         online tailnet peers advertising exit-node
      --up <EXIT_IP> [--authkey KEY|file:PATH] [--note …]   one slot pinned to that phone
      --sync [--authkey KEY|file:PATH] reconcile: one slot per online exit-node peer
      --down <slot> / --down-all
      --status / --check               (--check fetches each slot's egress IP + carrier/ASN)
      --on / --off                     master switch the pool reads
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_PATH = Path(__file__).resolve().parent.parent / "data" / "ts_egress.json"
_STATE_ROOT = Path(__file__).resolve().parent.parent / "data" / "ts-egress"
_LOCK = threading.Lock()
_ECHO_URL = "http://api.ipify.org?format=json"
_DEFAULT_BASE_PORT = 10800
_SOCK_WAIT = 10.0            # seconds to wait for userspace tailscaled to open its socket
_JOIN_TIMEOUT = 120         # seconds for `tailscale ... up` to complete the join
# status() does an egress-less aggregate but still walks running_slots -> cache ~60s
_CACHE: dict = {"ts": 0.0, "status": None}
_CACHE_TTL = 60.0
# the hot path (proxy_pool asks per fill) is TCP-only + a short cache
_RUN_CACHE: dict = {"ts": 0.0, "slots": []}
_RUN_TTL = 15.0

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


# ---- settings ------------------------------------------------------------------------------------
def _blank() -> dict:
    return {"enabled": False, "base_port": _DEFAULT_BASE_PORT, "cursor": 0, "slots": {}}


def _normalize(d: dict) -> dict:
    """Bring a loaded dict up to schema; slot keys become ints (JSON stringifies them on save)."""
    if not isinstance(d, dict):
        return _blank()
    d.setdefault("enabled", False)
    d["enabled"] = bool(d.get("enabled"))
    try:
        d["base_port"] = int(d.get("base_port") or _DEFAULT_BASE_PORT)
    except (TypeError, ValueError):
        d["base_port"] = _DEFAULT_BASE_PORT
    try:
        d["cursor"] = int(d.get("cursor") or 0)
    except (TypeError, ValueError):
        d["cursor"] = 0
    slots: dict = {}
    for k, v in (d.get("slots") or {}).items():
        try:
            si = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        try:
            port = int(v.get("port") or (d["base_port"] + si))
        except (TypeError, ValueError):
            port = d["base_port"] + si
        slots[si] = {"port": port,
                     "exit_ip": (v.get("exit_ip") or "").strip(),
                     "hostname": (v.get("hostname") or f"jf-egress-{si}").strip(),
                     "note": (v.get("note") or "").strip()}
    d["slots"] = slots
    return d


def load() -> dict:
    try:
        d = json.loads(_PATH.read_text())
    except Exception:
        return _blank()
    return _normalize(d if isinstance(d, dict) else {})


def save(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, _PATH)
    _invalidate()


def _invalidate() -> None:
    _CACHE["ts"] = 0.0
    _RUN_CACHE["ts"] = 0.0


def set_enabled(on: bool) -> dict:
    with _LOCK:
        d = load()
        d["enabled"] = bool(on)
        save(d)
        return d


# ---- pure helpers ---------------------------------------------------------------------------------
def local_socks(port: int) -> str:
    return f"socks5://127.0.0.1:{port}"


def slot_port(slot: int) -> int:
    return load()["base_port"] + int(slot)


def _statedir(slot: int) -> Path:
    return _STATE_ROOT / str(int(slot))


def up_argv(statedir, port: int) -> list[str]:
    """The exact userspace-`tailscaled` argv for a slot. PURE — no root (userspace = no TUN), SOCKS
    bound to 127.0.0.1 ONLY (global security rule 1)."""
    sd = str(statedir)
    return ["tailscaled", "-tun=userspace-networking",
            f"-socks5-server=127.0.0.1:{port}",
            f"-socket={sd}/tailscaled.sock",
            f"-state={sd}/tailscaled.state",
            f"-statedir={sd}"]


def join_argv(statedir, authkey: str, hostname: str, exit_ip: str) -> list[str]:
    """The exact `tailscale ... up` argv that auths the slot's node to the tailnet and PINS it to the
    phone's exit node. PURE — building it never stores or logs the key (the CLI masks it when echoing)."""
    sd = str(statedir)
    return ["tailscale", f"--socket={sd}/tailscaled.sock", "up",
            f"--authkey={authkey}", f"--hostname={hostname}",
            f"--exit-node={exit_ip}", "--exit-node-allow-lan-access",
            "--accept-routes=false", "--ssh=false"]


def _mask_key(argv: list[str]) -> list[str]:
    return [(a.split("=", 1)[0] + "=***" if a.startswith("--authkey=") else a) for a in argv]


def _scrub(s: str) -> str:
    return re.sub(r"(--authkey=|authkey=?)\S+", r"\1***", s or "")


def tcp_alive(server: str, timeout: float = 1.0) -> bool:
    try:
        u = urlsplit(server if "://" in server else "socks5://" + server)
        socket.create_connection((u.hostname or "127.0.0.1", int(u.port or 0)), timeout=timeout).close()
        return True
    except Exception:
        return False


# ---- tailscale peers ------------------------------------------------------------------------------
def _tailscale_status_json(timeout: float = 4.0, socket_path: str | None = None) -> dict:
    """`tailscale status --json`. With `socket_path` it queries THAT userspace node (on the key's
    tailnet) instead of the host's main node — the phones live on the key's tailnet, not the host's."""
    try:
        args = ["tailscale"] + ([f"--socket={socket_path}"] if socket_path else []) + ["status", "--json"]
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception:
        pass
    return {}


def _parse_exit_peers(st: dict) -> list[dict]:
    """Online peers advertising exit-node capability (`ExitNodeOption == True`) from a status dict.
    MUST be read from a node WITHOUT an exit-node pinned: a node whose ACTIVE exit node is a peer
    reports that peer as `ExitNode=true`/`ExitNodeOption=false`, so a pinned egress slot is a
    corrupt vantage — discovery uses a clean no-exit probe (see `_discovery_socket`)."""
    out = []
    for _k, p in (st.get("Peer") or {}).items():
        if not p.get("ExitNodeOption") or not p.get("Online"):
            continue
        ip = next((a for a in (p.get("TailscaleIPs") or []) if _IPV4.match(a)), "")
        if not ip:
            continue
        out.append({"name": (p.get("HostName") or p.get("DNSName") or "").split(".")[0],
                    "ip": ip, "os": p.get("OS") or "", "online": True})
    return out


def exit_node_peers(socket_path: str | None = None) -> list[dict]:
    """Every ONLINE tailnet peer that ADVERTISES exit-node capability. Each: {name, ip, os, online}.
    [] on failure. Reads via `socket_path` (a node on the key's tailnet) when given — the host's main
    node is on a DIFFERENT tailnet than the phones, so bare discovery there returns nothing."""
    try:
        return _parse_exit_peers(_tailscale_status_json(socket_path=socket_path))
    except Exception:
        return []


# ---- bring-up / tear-down (owner/sync-triggered; userspace, no sudo) ------------------------------
def _lowest_free_slot(d: dict) -> int:
    used = set(d.get("slots") or {})
    s = 0
    while s in used:
        s += 1
    return s


def _teardown(slot: int) -> None:
    """Best-effort: kill the slot's tailscaled + rm the dir. Match the FULL socket path
    (`<statedir>/tailscaled.sock`) — a bare state-dir path would prefix-match sibling slots
    (`…/ts-egress/1` is a prefix of `…/ts-egress/10`) and reap the wrong daemons."""
    statedir = _statedir(slot)
    try:
        subprocess.run(["pkill", "-f", f"{statedir}/tailscaled.sock"], capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        shutil.rmtree(statedir, ignore_errors=True)
    except Exception:
        pass


def _drop_slot(slot: int) -> None:
    _teardown(slot)
    with _LOCK:
        d = load()
        if int(slot) in d["slots"]:
            del d["slots"][int(slot)]
            save(d)


def up(exit_ip: str, authkey: str, *, slot: int | None = None, note: str = "") -> dict | None:
    """Bring up ONE egress slot pinned to `exit_ip`. Assigns the lowest free slot if `slot is None`.
    Spawns a userspace tailscaled in the background (no sudo), then joins + pins the exit node with a
    timeout. Records the slot in state. NEVER logs/returns the authkey. On ANY failure, best-effort
    tears down the half-started slot and returns None. Returns the slot record incl. `server`."""
    exit_ip = (exit_ip or "").strip()
    key = (authkey or "").strip()
    if not exit_ip or not key:
        return None
    with _LOCK:
        d = load()
        if slot is None:
            slot = _lowest_free_slot(d)
        slot = int(slot)
        port = d["base_port"] + slot
        hostname = f"jf-egress-{slot}"
        rec = {"port": port, "exit_ip": exit_ip, "hostname": hostname, "note": (note or "").strip()}
        d["slots"][slot] = rec           # reserve the slot before the slow subprocess work
        save(d)
    statedir = _statedir(slot)
    try:
        statedir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(up_argv(statedir, port), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        sock = statedir / "tailscaled.sock"
        deadline = time.time() + _SOCK_WAIT
        while not sock.exists() and time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("tailscaled exited before opening its socket")
            time.sleep(0.2)
        if not sock.exists():
            raise RuntimeError("tailscaled socket never appeared")
        r = subprocess.run(join_argv(statedir, key, hostname, exit_ip),
                           capture_output=True, text=True, timeout=_JOIN_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(_scrub(r.stderr) or "tailscale up failed")
    except Exception:
        _drop_slot(slot)
        return None
    _invalidate()
    return {"slot": slot, "server": local_socks(port), **rec}


def down(slot: int) -> bool:
    """Log the slot's ephemeral node out (so it deregisters), kill tailscaled, rm the state dir, drop
    from state. Guarded — always returns True (best-effort)."""
    slot = int(slot)
    sock = _statedir(slot) / "tailscaled.sock"
    try:
        if sock.exists():
            subprocess.run(["tailscale", f"--socket={sock}", "logout"],
                          capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    _drop_slot(slot)
    return True


def down_all() -> dict:
    n = 0
    for s in sorted(load().get("slots") or {}):
        try:
            if down(s):
                n += 1
        except Exception:
            pass
    try:
        _kill_probe(_STATE_ROOT / _PROBE_DIR)               # also reap a leftover discovery probe
    except Exception:
        pass
    return {"down": n}


# ---- the local-SOCKS pool (hot path: TCP-only, cached, never raises) ------------------------------
def running_slots(timeout: float = 1.0) -> list[dict]:
    """Slot records whose tailscaled socket EXISTS and whose local SOCKS port is tcp_alive. Cheap,
    cached ~15s so proxy_pool can consult it per fill. Each: {slot, server, port, exit_ip, hostname, note}."""
    now = time.time()
    if now - _RUN_CACHE["ts"] < _RUN_TTL:
        return list(_RUN_CACHE["slots"])
    out = []
    try:
        d = load()
        for slot in sorted(d.get("slots") or {}):
            rec = d["slots"][slot]
            if not (_statedir(slot) / "tailscaled.sock").exists():
                continue
            if not tcp_alive(local_socks(rec["port"]), timeout=timeout):
                continue
            out.append({"slot": slot, "server": local_socks(rec["port"]), **rec})
    except Exception:
        out = []
    _RUN_CACHE.update(ts=now, slots=out)
    return list(out)


def live_socks() -> list[str]:
    """What proxy_pool consumes: `socks5://127.0.0.1:<port>` for every running slot, when enabled.
    [] when disabled / none. Guarded — never raises on the fill hot path."""
    try:
        if not load().get("enabled"):
            return []
        return [r["server"] for r in running_slots()]
    except Exception:
        return []


# ---- reconcile / probe (owner-run) ----------------------------------------------------------------
_PROBE_DIR = "_probe"


def _kill_probe(sd) -> None:
    """Tear down the transient discovery node (logout so its ephemeral node deregisters, kill, rm)."""
    sd = Path(sd)
    sk = sd / "tailscaled.sock"
    try:
        if sk.exists():
            subprocess.run(["tailscale", f"--socket={sk}", "logout"], capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", f"{sd}/tailscaled.sock"], capture_output=True, timeout=10)
    except Exception:
        pass
    shutil.rmtree(sd, ignore_errors=True)


def _discovery_socket(authkey: str):
    """Return (socket_path, transient_statedir|None) for a CLEAN discovery node on the key's tailnet:
    a TRANSIENT userspace probe with NO exit-node pinned (so every exit-node-capable peer reports
    `ExitNodeOption=true` — a pinned egress slot would report its active exit node as `ExitNode` and
    hide it). The caller tears it down via `_kill_probe`. (None, None) on failure. Never raises."""
    key = (authkey or "").strip()
    if not key:
        return None, None
    sd = _STATE_ROOT / _PROBE_DIR
    try:
        _kill_probe(sd)                                      # clear any stale probe first
        sd.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(up_argv(sd, load()["base_port"] - 1), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        sk = sd / "tailscaled.sock"
        deadline = time.time() + _SOCK_WAIT
        while not sk.exists() and time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("probe tailscaled exited")
            time.sleep(0.2)
        if not sk.exists():
            raise RuntimeError("probe socket never appeared")
        argv = ["tailscale", f"--socket={sk}", "up", f"--authkey={key}",
                "--hostname=jf-egress-probe", "--accept-routes=false", "--ssh=false"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=_JOIN_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(_scrub(r.stderr) or "probe up failed")
        return str(sk), sd
    except Exception:
        _kill_probe(sd)
        return None, None


def sync(authkey: str) -> dict:
    """Reconcile desired vs running: desired = one slot per ONLINE exit-node peer (discovered on the
    KEY'S tailnet via a clean no-exit probe). Bring up the missing, reap slots whose exit_ip is no
    longer an online exit-node peer. REAP-SAFE: a failed/unhealthy discovery reaps NOTHING (a
    transient network blip must never nuke working slots). Guarded."""
    summary = {"desired": [], "brought_up": [], "reaped": [], "kept": [], "errors": 0}
    disc_sock, transient = _discovery_socket(authkey)
    if not disc_sock:                                        # no discovery node -> touch nothing
        summary["errors"] += 1
        return summary
    try:
        st = _tailscale_status_json(socket_path=disc_sock)
    except Exception:
        st = {}
    finally:
        if transient:
            _kill_probe(transient)
    if not st.get("Peer"):                                  # empty/partial netmap -> never reap
        summary["errors"] += 1                              # (the tailnet always has peers; a probe
        return summary                                      #  that shows none is not yet converged)
    peers = _parse_exit_peers(st)
    desired_ips = {p["ip"] for p in peers if p.get("ip")}
    summary["desired"] = sorted(desired_ips)
    _RUN_CACHE.update(ts=0.0, slots=[])             # force a fresh liveness read (a daemon may have died)
    slots_by_ip: dict = {}                          # exit_ip -> [slot record-keys]
    for slot, rec in (load().get("slots") or {}).items():
        if rec.get("exit_ip"):
            slots_by_ip.setdefault(rec["exit_ip"], []).append(slot)
    running_ips = {r["exit_ip"] for r in running_slots()}
    for ip, slots in list(slots_by_ip.items()):     # reap slots whose exit_ip isn't a desired online peer
        if ip not in desired_ips:                   # (an exit node that went offline is reaped even though
            for slot in slots:                      #  its LOCAL socks is still tcp-alive = egress-dead)
                try:
                    if down(slot):
                        summary["reaped"].append(ip)
                except Exception:
                    summary["errors"] += 1
    for ip in sorted(desired_ips):                  # ensure ONE RUNNING slot per desired peer
        if ip in running_ips:                       # (rebuild a dead/missing daemon — boot-safe + self-heal)
            summary["kept"].append(ip)
            continue
        for slot in slots_by_ip.get(ip, []):        # clear a stale record whose daemon is gone
            try:
                down(slot)
            except Exception:
                pass
        try:
            note = next((p.get("name") for p in peers if p.get("ip") == ip), "") or ""
            if up(ip, authkey, note=note):
                summary["brought_up"].append(ip)
            else:
                summary["errors"] += 1
        except Exception:
            summary["errors"] += 1
    return summary


def asn_of(ip: str, timeout: float = 5.0) -> str:
    from backend.tools import mobile_proxy
    return mobile_proxy.asn_of(ip, timeout=timeout)


def _echo_through(server: str, timeout: float = 8.0, echo=None):
    """Egress IP seen THROUGH the slot's local SOCKS (or None). `echo` injectable for tests."""
    if echo is not None:
        return echo(server)
    import httpx
    try:
        r = httpx.get(_ECHO_URL, proxy=server, timeout=timeout)
        if r.status_code == 200:
            return (r.json() or {}).get("ip") or None
    except Exception:
        return None
    return None


def check(echo=None) -> list[dict]:
    """For each running slot, fetch the egress IP THROUGH its local SOCKS + its carrier/ASN — so the
    owner can confirm each slot shows its phone's MOBILE IP. `echo` injectable for tests."""
    out = []
    for r in running_slots():
        egress = _echo_through(r["server"], timeout=15.0, echo=echo)   # first hop through a fresh
        if egress is None:                                             # exit-node is cold — one retry
            egress = _echo_through(r["server"], timeout=15.0, echo=echo)
        out.append({**r, "egress": egress, "asn": (asn_of(egress) if egress else "")})
    return out


def status(force: bool = False) -> dict:
    """Aggregate for health/UI, cached ~60s: {enabled, n_slots, n_running, slots:[…], base_port}.
    No egress fetch here (that's check())."""
    now = time.time()
    if not force and _CACHE["status"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return json.loads(json.dumps(_CACHE["status"]))
    d = load()
    running_ports = {r["slot"] for r in running_slots()}
    slots = []
    for slot in sorted(d.get("slots") or {}):
        rec = d["slots"][slot]
        slots.append({"slot": slot, "port": rec["port"], "exit_ip": rec["exit_ip"],
                      "hostname": rec["hostname"], "note": rec["note"],
                      "running": slot in running_ports, "egress": None})
    st = {"enabled": bool(d.get("enabled")), "n_slots": len(slots),
          "n_running": sum(1 for s in slots if s["running"]),
          "slots": slots, "base_port": int(d.get("base_port"))}
    _CACHE.update(ts=now, status=json.loads(json.dumps(st)))
    return st


# ---- CLI -----------------------------------------------------------------------------------------
def _read_authkey(arg: str | None) -> str:
    """Accept the key inline OR as `file:PATH` (read+strip) — like tailscale does, so the owner never
    pastes it on a shell line."""
    if not arg:
        return ""
    if arg.startswith("file:"):
        try:
            return Path(arg[5:]).read_text().strip()
        except Exception:
            return ""
    return arg.strip()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Exit-node egress-мост (телефон-exit-node → локальный SOCKS5 → пул) — статус/управление")
    ap.add_argument("--peers", action="store_true", help="online tailnet peers advertising exit-node")
    ap.add_argument("--up", metavar="EXIT_IP", help="bring up one slot pinned to this phone's tailnet IP")
    ap.add_argument("--sync", action="store_true", help="reconcile: one slot per online exit-node peer")
    ap.add_argument("--authkey", metavar="KEY|file:PATH", help="reusable tailnet auth key (or file:PATH)")
    ap.add_argument("--note", default="")
    ap.add_argument("--down", metavar="SLOT", type=int, help="tear down one slot")
    ap.add_argument("--down-all", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true", help="egress IP + carrier per running slot")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()

    if a.on or a.off:
        d = set_enabled(bool(a.on))
        print(json.dumps({"enabled": d["enabled"], "base_port": d["base_port"],
                          "n_slots": len(d["slots"])}, ensure_ascii=False))

    if a.peers:
        disc, transient = _discovery_socket(_read_authkey(a.authkey)) if a.authkey else (None, None)
        try:
            print(json.dumps(exit_node_peers(socket_path=disc), ensure_ascii=False, indent=1))
        finally:
            if transient:
                _kill_probe(transient)
        return

    if a.up:
        rec = up(a.up, _read_authkey(a.authkey), note=a.note)
        # never echo the key: mask the argv we WOULD run for context
        print("argv:", " ".join(_mask_key(join_argv(_statedir(rec["slot"] if rec else 0),
                                                     _read_authkey(a.authkey) or "***",
                                                     (rec or {}).get("hostname", "jf-egress-?"), a.up))))
        print(json.dumps(rec or {"error": "up failed (see server; key masked)"}, ensure_ascii=False))
        return

    if a.sync:
        print(json.dumps(sync(_read_authkey(a.authkey)), ensure_ascii=False, indent=1))
        return

    if a.down is not None:
        print(json.dumps({"down": down(a.down), "slot": a.down}, ensure_ascii=False))
        return

    if a.down_all:
        print(json.dumps(down_all(), ensure_ascii=False))
        return

    if a.check:
        for r in check():
            print(f"[slot {r['slot']}] {r['server']:<26} exit={r['exit_ip']:<15} "
                  f"{r.get('egress') or '-':<16} {r.get('asn') or ''}")
        return

    # default / --status
    st = status(force=True)
    print(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
