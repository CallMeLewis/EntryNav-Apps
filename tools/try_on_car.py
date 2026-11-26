#!/usr/bin/env python3
"""Tries the Rust ports of the workers on a car, from a laptop, beside the installed C workers. It installs nothing
and needs no Flare Drive.

    uv run --no-project --with paramiko tools/try_on_car.py --host HEAD_UNIT_ADDRESS [--worker all] [--seconds 20]

It logs in over SSH as root, with the password from HEADUNIT_PASSWORD or a prompt. The password is never printed or
saved. Then it:
1. reads the firmware line of /etc/version, and checks that /tmp is writable, lets programs run and has room. It stops
   if an EntryNav-Apps worker holds the diagnostic lease (UDP 127.0.0.1:42861) or is running, because then an app's
   page is open in iDrive and the two would take turns on the gateway;
2. makes a folder with mktemp -d /tmp/ena-rust.XXXXXX, uploads build/rust's workers into it (make rust builds them)
   and checks each one's SHA-256 on the car;
3. runs each read-only job the apps give these workers, the Rust worker first and then the installed C one, with the
   same arguments: engine-codes --host GATEWAY --codes, as Engine Fault Codes runs it, ena-gateway check --host
   GATEWAY and ena-gateway discover, as setup.sh gateway runs them, and bm3-maps --host GATEWAY --status, as Bootmod3
   Suite runs it when its page opens. That identifies the ROM, defining the dynamic DID F301 in the engine computer's
   RAM (2C) and reading it (22) or its memory (23), then reads the active map; it writes no memory. mhd-maps has no
   read-only job, since every one asks MHD's code for something, so it runs only as mhd-maps --host GATEWAY, which
   sends nothing and prints its usage; without MHD+ Suite installed it is left out, as there is nothing to compare
   with. Each worker but ena-gateway also gets --gateway-file in the folder, so that a worker that finds the gateway
   at a new address saves it there instead of in the apps' settings;
4. runs the gauge workers as BoostDeck and ShiftDeck run them (boostdeck-collector --host GATEWAY --session and
   shift-deck --host GATEWAY), the Rust one and then the C one, for --seconds each, since two can't hold the gateway at
   once. Each sends its frames to ena-capture, a listener uploaded with them, on a free port of its own (--udp-port),
   so the apps' ports stay untouched; each also gets --gateway-file in the folder. Then SIGTERM stops it, as the app
   does. Every second the script checks that no app's worker has started; if one has, it stops its own and ends;
5. prints both results side by side, with exit code, run time and peak memory, and whether they match. For the gauge
   workers it compares the frames: their format, the readings given, the frame rate (within RATE_TOLERANCE) and each
   reading's median (within its tolerance in READINGS or SPEEDS, as live readings change between runs);
6. deletes its folder, also after an error or Ctrl-C.

It never writes outside its folder, never changes /opt, restarts nothing, and refuses every argument that writes to the
engine computer (engine-codes --clear, bm3-maps --map, --burbles and --sound, mhd-maps --map and --cooling) and every
one that isn't one of the workers' read-only ones. --worker-args runs one worker with other read-only arguments.
Exit 0 when every run matched, 1 when one differed, 2 for refused arguments, 3 when a check stopped it, 130 for Ctrl-C.
"""

import argparse
import base64
import difflib
import getpass
import hashlib
import ipaddress
import os
import re
import shlex
import signal
import statistics
import struct
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
ONE_SHOT = ("engine-codes", "ena-gateway", "bm3-maps", "mhd-maps")
GAUGES = ("boostdeck-collector", "shift-deck")
WORKERS = (*ONE_SHOT, *GAUGES)
CAPTURE = "ena-capture"  # records what a gauge worker sends; uploaded with the gauge workers
PLUGINS = "/opt/hmi/data/ro/Plugins"
# Where each app has its C worker (src/paths.h), and the plugin folders setup.sh puts ena-gateway in.
INSTALLED = {"engine-codes": [f"{PLUGINS}/EngineCodes/engine-codes"],
             "ena-gateway": [f"{PLUGINS}/{app}/ena-gateway"
                             for app in ("EngineCodes", "BoostDeck", "ShiftDeck", "Bm3Maps", "MhdMaps")],
             "bm3-maps": [f"{PLUGINS}/Bm3Maps/bm3-maps"],
             "mhd-maps": [f"{PLUGINS}/MhdMaps/mhd-maps"],
             "boostdeck-collector": [f"{PLUGINS}/BoostDeck/boostdeck-collector"],
             "shift-deck": [f"{PLUGINS}/ShiftDeck/shift-deck"]}
GATEWAY_FILE = "/var/local/data/EntryNavApps/gateway-ip.txt"
GAUGE_WORKERS = ("boostdeck-collector", "shift-deck", "engine-codes", "bm3-maps", "mhd-maps")
FOLDER = re.compile(r"^/tmp/ena-rust\.[A-Za-z0-9]{6}$")
GATEWAY = "GATEWAY"  # replaced by the saved gateway address
PORT = "@PORT@"  # replaced on the head unit by ena-capture's port
# The jobs the apps give each worker: what it does, and its arguments.
JOBS = {"engine-codes": [("read the engine's fault codes, as Engine Fault Codes does", ["--host", GATEWAY, "--codes"])],
        "ena-gateway": [("check the route to the gateway, as setup.sh gateway does", ["check", "--host", GATEWAY]),
                        ("look for the gateway on vbr0, as setup.sh gateway does", ["discover"])],
        # Identifies the ROM (2C defines DID F301 in the DME's RAM, 22 and 23 read) and reads the active map: no write.
        "bm3-maps": [("identify the ROM and read the active map, as Bootmod3 Suite does when its page opens",
                      ["--host", GATEWAY, "--status"])],
        # Every job of mhd-maps writes (--map, --cooling), so only its answer to a run without one, which sends nothing.
        "mhd-maps": [("refuse a run without a map or Max Cooling, sending nothing (its only jobs write)",
                      ["--host", GATEWAY])],
        "boostdeck-collector": [("stream the eight readings, as BoostDeck does", ["--host", GATEWAY, "--session"])],
        "shift-deck": [("stream engine and road speed, as ShiftDeck does", ["--host", GATEWAY])]}
# How each upload is tried before it runs: these arguments make it exit 2 at once, with its usage when it has one.
PROBES = {"engine-codes": ([], True), "ena-gateway": ([], True), "bm3-maps": ([], True), "mhd-maps": ([], True),
          "boostdeck-collector": (["--help"], True),
          "shift-deck": (["--help"], False), CAPTURE: (["--help"], True)}
LEASE_HELD = "Diagnostics busy"
# The arguments that make a worker write to the engine computer, refused whatever else is given.
WRITES = {"bm3-maps": {"--map": "writes bootmod3's map slot to the engine computer",
                       "--burbles": "writes the burble table and switch to the engine computer",
                       "--sound": "sends the engine computer an Active Sound command"},
          "mhd-maps": {"--map": "asks the engine computer's MHD code to switch maps",
                       "--cooling": "sends the engine computer's MHD code a Max Cooling command"}}
# What is tried only when its app is installed, with the reason it isn't tried otherwise.
NEEDS_APP = {"mhd-maps": "MHD+ Suite isn't installed on this car, so there is no C mhd-maps to compare with. mhd-maps "
                         "is left out: only a car with MHD+ Suite installed can try it."}
EXIT_MISMATCH, EXIT_REFUSED, EXIT_STOPPED, EXIT_INTERRUPTED = 1, 2, 3, 130

# How far the C and Rust gauge runs may differ. They run one after the other, so live readings change between them:
# each reading is compared by its median over the run, against a tolerance in its own unit.
RATE_TOLERANCE = 0.15  # frames a second, as a share of the C's
GIVEN_TOLERANCE = 0.25  # the share of frames that carry a reading
READINGS = [("Boost", "psi", 3.0), ("AFR", "", 1.5), ("Timing", "deg", 8.0), ("Intake air", "C", 5.0),
            ("Transmission", "C", 5.0), ("Rail pressure", "bar", 40.0), ("Oil pressure", "bar", 1.0),
            ("Coolant", "C", 5.0)]
SPEEDS = [("Engine speed", "rpm", 250.0), ("Road speed", "km/h", 3.0)]
STALL_MS = 400  # a gap between frames this long is a stall, which the summary counts


class Stop(Exception):
    """A check failed: say why and end, after cleaning up."""


# ---- what may run ------------------------------------------------------------------------------------------------

def address(value):
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def number(value, lo, hi):
    return value.isdigit() and lo <= int(value) <= hi


def refused(worker, args):
    """Why args aren't a read-only run of worker, or None. Only the options the workers read with are taken, each
    once. The gauge workers' --udp-port and --gateway-file are the script's own, and --cycles and --demo aren't how
    the apps run them."""
    if "--clear" in args:
        return "--clear clears the engine computer's fault memory. This script only reads."
    for option, what in WRITES.get(worker, {}).items():
        if option in args:
            return f"{option} {what}. This script only reads."
    if worker in GAUGES:
        options = {"--host": address, "--port": lambda v: number(v, 1, 65535)}
        flags, rest = ({"--session", "--no-trans", "--trace"} if worker == "boostdeck-collector" else set()), list(args)
        if "--host" not in rest:
            return f"{worker} needs --host"
        if worker == "boostdeck-collector" and "--session" not in rest:
            return "boostdeck-collector needs --session: without it, it doesn't take the diagnostic lease"
    elif worker == "engine-codes":
        options = {"--host": address, "--port": lambda v: number(v, 1, 65535)}
        flags, rest = {"--codes"}, list(args)
        if "--codes" not in rest:
            return "engine-codes needs --codes, its read-only job"
    elif worker == "bm3-maps":
        options = {"--host": address, "--port": lambda v: number(v, 1, 65535),
                   "--marker": lambda v: re.fullmatch(r"[0-9A-Fa-f]{8}", v) is not None}
        flags, rest = {"--status", "--dump"}, list(args)
        if "--status" not in rest:
            return "bm3-maps needs --status, its read-only job"
    elif worker == "mhd-maps":
        options = {"--host": address, "--port": lambda v: number(v, 1, 65535)}
        flags, rest = set(), list(args)
    elif worker == "ena-gateway":
        if not args or args[0] not in ("discover", "check"):
            return "ena-gateway takes discover or check, then its options"
        if args[0] == "check":
            options = {"--host": address, "--port": lambda v: number(v, 1, 65535)}
            if "--host" not in args:
                return "ena-gateway check needs --host"
        else:
            options = {"--dev": lambda v: re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", v) is not None, "--to": address,
                       "--port": lambda v: number(v, 1, 65535), "--wait": lambda v: number(v, 100, 10000)}
        flags, rest = set(), list(args[1:])
    else:
        return f"{worker} isn't one of {', '.join(WORKERS)}"
    seen = set()
    while rest:
        option = rest.pop(0)
        if option in seen:
            return f"{option} is given twice"
        seen.add(option)
        if option in flags:
            continue
        if option not in options:
            return f"{option} isn't a read-only option of {worker}"
        if not rest or not options[option](rest[0]):
            return f"{option} needs a valid value"
        rest.pop(0)
    return None


# ---- the head unit -------------------------------------------------------------------------------------------------

class HeadUnit:
    def __init__(self, host, port, password):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(ShowKey())
        self.client.connect(host, port=port, username="root", password=password, look_for_keys=False,
                            allow_agent=False, timeout=10, banner_timeout=15, auth_timeout=15)

    def run(self, script, data=None, timeout=60):
        """(exit code, stdout bytes, stderr bytes) of script, run by the head unit's shell. It waits in short sleeps,
        which Ctrl-C interrupts on Windows too."""
        channel = self.client.get_transport().open_session()
        channel.exec_command(script)
        if data is not None:
            channel.sendall(data)
        channel.shutdown_write()
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        while True:
            if channel.recv_ready():
                out += channel.recv(65536)
            elif channel.recv_stderr_ready():
                err += channel.recv_stderr(65536)
            # The exit status isn't held back by SSH's flow control as output is, so on a slow link it can arrive while
            # output is still on its way: stop only once the head unit has also ended the output (EOF).
            elif channel.exit_status_ready() and channel.eof_received:
                break
            elif time.monotonic() > deadline:
                channel.close()
                raise TimeoutError
            else:
                time.sleep(0.02)
        code = channel.recv_exit_status()
        channel.close()
        return code, bytes(out), bytes(err)

    def text(self, script, timeout=60):
        code, out, err = self.run(script, timeout=timeout)
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    def close(self):
        self.client.close()


class ShowKey(paramiko.MissingHostKeyPolicy):
    """Accepts the head unit's key without saving it, and shows its fingerprint to compare with an earlier run."""

    def missing_host_key(self, client, hostname, key):
        digest = hashlib.sha256(key.asbytes()).digest()
        print(f"Head unit's SSH key: {key.get_name()} SHA256:{base64.b64encode(digest).decode().rstrip('=')}")


def facts(lines):
    """KEY=VALUE lines as a dict of lists."""
    found = {}
    for line in lines.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found.setdefault(key, []).append(value)
    return found


BUSY_CHECK = f"""
awk 'NR > 1 && ($2 == "0100007F:A76D" || $2 == "00000000:A76D") {{ held = 1 }}
     END {{ print "LEASE=" (held ? "held" : "free") }}' /proc/net/udp
for name in {' '.join(GAUGE_WORKERS)}; do
    pids=$(pidof "$name" 2>/dev/null)
    [ -n "$pids" ] && echo "RUNNING=$name $pids"
done
"""


def busy(unit):
    """Why the gateway is in use on the car, or None."""
    _, out, _ = unit.text(BUSY_CHECK)
    found = facts(out)
    running = found.get("RUNNING", [])
    if found.get("LEASE") != ["free"]:
        why = "An EntryNav-Apps worker holds the diagnostic lease (UDP 127.0.0.1:42861)"
        return why + (f": {', '.join(running)}" if running else "") + "."
    if running:
        return f"An EntryNav-Apps worker is running ({', '.join(running)})."
    return None


def preflight(unit, workers, need_kb):
    """The head unit's details, or Stop."""
    paths = " ".join(shlex.quote(p) for w in workers for p in INSTALLED[w])
    _, out, _ = unit.text(f"""
read -r firmware rest </etc/version 2>/dev/null && echo "FIRMWARE=$firmware"
echo "KERNEL=$(uname -r)"
if [ -d /tmp ] && [ -w /tmp ]; then echo TMP_WRITABLE=1; else echo TMP_WRITABLE=0; fi
awk '$2 == "/tmp" {{ print "TMP_OPTIONS=" $4 }}' /proc/mounts
df -k /tmp 2>/dev/null | awk 'END {{ print "TMP_FREE_KB=" $(NF-2) }}'
awk '$1 == "MemFree:" {{ print "MEM_FREE_KB=" $2 }}' /proc/meminfo
for p in {paths}; do [ -x "$p" ] && echo "INSTALLED=$p"; done
command -v openssl >/dev/null 2>&1 && echo OPENSSL=1
""")
    found = facts(out)
    firmware = (found.get("FIRMWARE") or [""])[0]
    if not firmware:
        raise Stop("/etc/version has no firmware line: this doesn't look like an EntryNav2 head unit.")
    print(f"Firmware {firmware}, kernel {found.get('KERNEL', ['?'])[0]}, "
          f"{found.get('MEM_FREE_KB', ['?'])[0]} kB of memory free")
    options = (found.get("TMP_OPTIONS") or [""])[-1].split(",")
    if found.get("TMP_WRITABLE") != ["1"] or "ro" in options:
        raise Stop("/tmp isn't writable.")
    if "noexec" in options:
        raise Stop("/tmp is mounted noexec, so the workers can't run from it.")
    free = (found.get("TMP_FREE_KB") or ["0"])[0]
    if not free.isdigit() or int(free) < need_kb:
        raise Stop(f"/tmp has {free} kB free, and the workers need {need_kb} kB.")
    print(f"/tmp is writable, allows programs and has {free} kB free ({need_kb} kB needed)")
    why = busy(unit)
    if why:
        raise Stop(why + " Close the app's page in iDrive (BoostDeck, ShiftDeck or another EntryNav-Apps app), wait "
                         "a few seconds and run this again. Nothing was uploaded.")
    print("No EntryNav-Apps worker is running or holds the diagnostic lease")
    installed = {}
    for worker in workers:
        installed[worker] = next((p for p in INSTALLED[worker] if p in found.get("INSTALLED", [])), None)
        print(f"Installed C {worker}: {installed[worker] or 'not found, so only the Rust worker runs'}")
    return installed, found.get("OPENSSL") == ["1"]


def saved_gateway(unit):
    """The gateway address the apps use, read as the plugin reads it (read_gateway in src/diagnostic_worker.h)."""
    code, data, _ = unit.run(f"cat {GATEWAY_FILE}")
    if code:
        return None
    first, _, rest = data.partition(b"\n")
    if rest or len(first) + 1 >= 32:
        return None
    text = first.rstrip(b"\r").decode(errors="replace")
    try:
        value = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    bits = int(value)  # gateway_address_ok in src/diagnostic_lease.h
    if bits >> 16 == 0xa9fe:
        usable = 1 <= (bits >> 8 & 0xff) <= 254
    else:
        usable = bits >> 24 == 10 or bits >> 20 == 0xac1 or bits >> 16 == 0xc0a8
    return text if usable else None


def make_folder(unit):
    code, out, err = unit.text("mktemp -d /tmp/ena-rust.XXXXXX")
    folder = out.strip()
    if code or not FOLDER.match(folder):
        raise Stop(f"mktemp -d didn't make a folder in /tmp: {err.strip() or folder!r}")
    return folder


def upload(unit, folder, name, data, openssl):
    target = f"{folder}/{name}"
    code, _, err = unit.run(f"cat > {shlex.quote(target)} && chmod 755 {shlex.quote(target)}", data=data)
    if code:
        raise Stop(f"Uploading {name} failed: {err.decode(errors='replace').strip()}")
    expected = hashlib.sha256(data).hexdigest()
    if openssl:
        code, out, _ = unit.text(f"openssl dgst -sha256 {shlex.quote(target)}")
        actual, how = (out.split()[-1].lower() if out.split() else ""), "openssl on the car"
    else:
        code, back, _ = unit.run(f"cat {shlex.quote(target)}")
        actual, how = hashlib.sha256(back).hexdigest(), "read back"
    if code or actual != expected:
        raise Stop(f"{name} on the car has SHA-256 {actual or 'unknown'}, not {expected}: the upload is damaged.")
    print(f"Uploaded {name}: {len(data)} bytes, SHA-256 {expected} matches ({how})")
    probe, usage = PROBES[name]  # it prints its usage, if it has one, and exits 2
    code, _, err = unit.text(" ".join(shlex.quote(part) for part in [target, *probe]))
    if code != 2 or (usage and not err.startswith("Usage:")) or (not usage and err):
        raise Stop(f"The head unit couldn't run {name} (exit {code}): {err.strip()[:200]}")


RUN = """
d={folder}
rm -f "$d/out" "$d/err" "$d/pid" "$d/moved-gateway.txt"
read -r start rest </proc/uptime
env -i LD_LIBRARY_PATH=/lib:/usr/lib {command} </dev/null >"$d/out" 2>"$d/err" &
pid=$!
peak=
n=0
# Peak memory: VmHWM, sampled with the shell's own read, at once and then every 10 ms. It counts only once the process
# is the worker, not env before its exec.
while :; do
    state=
    name=
    if [ -r "/proc/$pid/status" ]; then
        while read -r key value rest; do
            case $key in
                Name:) name=$value ;;
                State:) state=$value ;;
                VmHWM:) [ "$name" = {name} ] && peak=$value ;;
            esac
        done 2>/dev/null <"/proc/$pid/status"
    fi
    if [ $n = 0 ] && read -r stat 2>/dev/null <"/proc/$pid/stat"; then
        # The pid and its start time, so that a cleanup after Ctrl-C signals this process and no other.
        set -- $stat
        shift 21
        echo "$pid $1" >"$d/pid"
    fi
    case $state in ''|Z|X) break ;; esac
    n=$((n + 1))
    [ $n -gt 50 ] && usleep 10000
done
wait "$pid"
code=$?
read -r end rest </proc/uptime
rm -f "$d/pid"
echo "CODE=$code"
echo "START=$start"
echo "END=$end"
echo "PEAK_KB=$peak"
[ -f "$d/moved-gateway.txt" ] && echo "MOVED=$(cat "$d/moved-gateway.txt")"
exit 0
"""


def run_worker(unit, folder, program, args, timeout):
    """One run: exit code, stdout, stderr, seconds, peak kB, and a new gateway address if the worker found one."""
    command = " ".join(shlex.quote(part) for part in [program, *args])
    name = shlex.quote(program.rsplit("/", 1)[-1][:15])  # the kernel keeps 15 characters of it
    code, out, err = unit.text(RUN.format(folder=shlex.quote(folder), command=command, name=name), timeout=timeout)
    if code:
        raise Stop(f"Running {program} failed on the head unit: {err.strip()[:200]}")
    found = facts(out)
    _, stdout, stderr = unit.run(f'cat "{folder}/out"; cat "{folder}/err" >&2')
    start, end = float(found["START"][0]), float(found["END"][0])
    peak = (found.get("PEAK_KB") or [""])[0]
    # A usage line names the program, which is in another folder for each build.
    stderr = stderr.replace(program.encode(), b"WORKER")
    return {"code": int(found["CODE"][0]), "stdout": stdout, "stderr": stderr, "seconds": end - start,
            "peak": f"{peak} kB" if peak.split() and peak.split()[0].isdigit() else "not readable",
            "moved": (found.get("MOVED") or [None])[0]}


GAUGE_RUN = """
d={folder}
rm -f "$d/out" "$d/err" "$d/pid" "$d/capture" "$d/capture-err" "$d/moved-gateway.txt"
# Each process this starts, as its pid and start time, so that a cleanup after Ctrl-C signals these and no others.
record() {{
    stat=
    read -r stat 2>/dev/null <"/proc/$1/stat" || return
    set -- $stat
    p=$1
    shift 21
    echo "$p $1" >>"$d/pid"
}}
status() {{
    value=
    while read -r key field rest; do
        [ "$key" = "$2" ] && value=$field
    done 2>/dev/null <"/proc/$1/status"
    echo "$value"
}}
env -i "$d/{capture}" --seconds {limit} </dev/null >"$d/capture" 2>"$d/capture-err" &
capture=$!
record $capture
port=
n=0
while [ -z "$port" ] && [ $n -lt 250 ]; do
    read -r key value rest 2>/dev/null <"$d/capture"
    [ "$key" = PORT ] && port=$value
    [ -z "$port" ] && usleep 20000
    n=$((n + 1))
done
case $port in
    ''|*[!0-9]*) kill -TERM $capture 2>/dev/null; wait $capture; rm -f "$d/pid"; echo "NOPORT=1"; exit 0 ;;
esac
read -r start rest </proc/uptime
env -i LD_LIBRARY_PATH=/lib:/usr/lib {command} </dev/null >"$d/out" 2>"$d/err" &
pid=$!
record $pid
others=
s=0
while [ $s -lt {seconds} ] && [ -z "$others" ]; do
    sleep 1
    s=$((s + 1))
    case $(status $pid State:) in ''|Z|X) break ;; esac
    # An app's worker that started meanwhile: an EntryNav-Apps page opened in iDrive.
    for name in {gauge_workers}; do
        for p in $(pidof "$name" 2>/dev/null); do
            [ "$p" != "$pid" ] && others="$others $name:$p"
        done
    done
done
peak=$(status $pid VmHWM:)
kill -TERM $pid 2>/dev/null
wait $pid
code=$?
read -r end rest </proc/uptime
usleep 200000
kill -TERM $capture 2>/dev/null
wait $capture
rm -f "$d/pid"
echo "CODE=$code"
echo "START=$start"
echo "END=$end"
echo "PEAK_KB=$peak"
echo "OTHERS=$others"
[ -f "$d/moved-gateway.txt" ] && echo "MOVED=$(cat "$d/moved-gateway.txt")"
exit 0
"""


def run_gauge(unit, folder, program, args, seconds):
    """One gauge run of seconds: as run_worker, with the frames ena-capture recorded as [(arrival ms, bytes)]."""
    command = " ".join(shlex.quote(part) for part in [program, *args]).replace(PORT, '"$port"')
    script = GAUGE_RUN.format(folder=shlex.quote(folder), capture=CAPTURE, limit=seconds + 30, command=command,
                              seconds=seconds, gauge_workers=" ".join(GAUGE_WORKERS))
    code, out, err = unit.text(script, timeout=seconds + 60)
    if code:
        raise Stop(f"Running {program} failed on the head unit: {err.strip()[:200]}")
    found = facts(out)
    if found.get("NOPORT"):
        raise Stop(f"{CAPTURE} didn't start on the head unit, so nothing could record the frames.")
    others = (found.get("OTHERS") or [""])[0].split()
    if others:
        raise Stop(f"An app's worker started during the run ({', '.join(others)}): an EntryNav-Apps page opened in "
                   "iDrive. This script stopped its own worker. Close the page and run this again.")
    _, stdout, stderr = unit.run(f'cat "{folder}/out"; cat "{folder}/err" >&2')
    _, recorded, _ = unit.run(f'cat "{folder}/capture"', timeout=120)
    _, size, _ = unit.text(f'wc -c <"{folder}/capture"')
    if size.strip() != str(len(recorded)):
        raise Stop(f"The recording of {program} came back short ({len(recorded)} of {size.strip()} bytes): the "
                   "connection to the head unit is unsteady. Run this again.")
    start, end = float(found["START"][0]), float(found["END"][0])
    peak = (found.get("PEAK_KB") or [""])[0]
    return {"code": int(found["CODE"][0]), "stdout": stdout, "stderr": stderr, "seconds": end - start,
            "peak": f"{peak} kB" if peak.isdigit() else "not readable", "moved": (found.get("MOVED") or [None])[0],
            "frames": captured(recorded)}


def captured(data):
    """ena-capture's lines, after its PORT line: [(arrival ms, datagram bytes)]."""
    frames = []
    for line in data.decode(errors="replace").splitlines()[1:]:
        stamp, _, hexed = line.partition(" ")
        try:
            frames.append((int(stamp), bytes.fromhex(hexed)))
        except ValueError:
            continue
    return frames


def pace(times):
    """Frames a second, the longest gap in ms and the stalls (gaps of STALL_MS or more) of arrival times in ms."""
    gaps = [b - a for a, b in zip(times, times[1:])]
    rate = (len(times) - 1) * 1000 / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
    return rate, max(gaps, default=0), sum(gap >= STALL_MS for gap in gaps)


def gauge_summary(worker, frames):
    """What the app would have seen: frame counts, faults in the frames, the pace, and per reading the share of frames
    that carry it and its median."""
    good, bad, breaks, off_clock, demo, times = 0, 0, 0, 0, 0, []
    names = READINGS if worker == "boostdeck-collector" else SPEEDS
    values = [[] for _ in names]
    previous = None
    for arrived, data in frames:
        if worker == "boostdeck-collector":
            text = data.decode(errors="replace")
            fields = text.split()
            try:
                if len(fields) != 21 or fields[0] != "NBG3" or not text.endswith("\n"):
                    raise ValueError
                seq, flag, valid, sent = (int(field) for field in fields[1:5])
                readings = [float(fields[5 + 2 * i]) if valid >> i & 1 else None for i in range(len(names))]
            except ValueError:
                bad += 1
                continue
            breaks += previous is not None and seq != previous + 1
            previous, demo = seq, demo + (flag != 0)
        else:
            if len(data) != 16:
                bad += 1
                continue
            sent, rpm, kph = struct.unpack("<Qii", data)
            readings = [rpm if rpm >= 0 else None, kph if kph >= 0 else None]
            breaks += previous is not None and sent < previous
            previous = sent
        good += 1
        times.append(arrived)
        off_clock += abs(arrived - sent) > 500  # stamped with CLOCK_MONOTONIC, the clock ena-capture reads
        for i, value in enumerate(readings):
            if value is not None:
                values[i].append(value)
    rate, longest, stalls = pace(times)
    return {"frames": good, "bad": bad, "breaks": breaks, "off clock": off_clock, "demo": demo, "rate": rate,
            "longest": longest, "stalls": stalls,
            "readings": [(len(v) / good if good else 0.0, statistics.median(v) if v else None) for v in values]}


def refusals(worker, stderr):
    """The lines on stderr that say something went wrong: for the collector, all but its first line (what it is) and
    the gateway moving; for shift-deck, every line, since it prints nothing."""
    found = lines(stderr)
    if worker == "boostdeck-collector":
        found = [line for line in found[1:] if not line.startswith("The gateway moved")]
    return sorted(set(found))


def gauge_problems(worker, rust, c):
    """Why the two runs don't match, or [] when they do."""
    problems = []
    summaries = {"Rust": gauge_summary(worker, rust["frames"]), "C": gauge_summary(worker, c["frames"])}
    for build, result in (("Rust", rust), ("C", c)):
        summary = summaries[build]
        if result["code"] != 0:
            problems.append(f"the {build} worker exited {result['code']}, not 0 after SIGTERM")
        if not summary["frames"]:
            problems.append(f"the {build} worker sent no frames")
        faults = [f"{summary[key]} {what}" for key, what in (("bad", "unreadable"), ("breaks", "out of order"),
                                                              ("off clock", "stamped off the monotonic clock"),
                                                              ("demo", "marked demo")) if summary[key]]
        if faults:
            problems.append(f"the {build} worker's frames: {', '.join(faults)}")
    if refusals(worker, rust["stderr"]) != refusals(worker, c["stderr"]):
        problems.append("different errors on stderr")
    r, k = summaries["Rust"], summaries["C"]
    if k["rate"] and abs(r["rate"] - k["rate"]) > RATE_TOLERANCE * k["rate"]:
        problems.append(f"{r['rate']:.1f} frames a second against the C's {k['rate']:.1f}, more than "
                        f"{RATE_TOLERANCE:.0%} apart")
    names = READINGS if worker == "boostdeck-collector" else SPEEDS
    for (name, unit_name, tolerance), (r_share, r_median), (c_share, c_median) in zip(names, r["readings"],
                                                                                      k["readings"]):
        if abs(r_share - c_share) > GIVEN_TOLERANCE:
            problems.append(f"{name} in {r_share:.0%} of the Rust frames and {c_share:.0%} of the C's")
        elif r_median is not None and c_median is not None and abs(r_median - c_median) > tolerance:
            problems.append(f"{name}: median {r_median:.1f} against the C's {c_median:.1f}, more than "
                            f"{tolerance:g} {unit_name} apart".replace("  ", " "))
    return problems


def compare_gauges(worker, title, args, rust, c, folder, c_path, seconds):
    print(f"\n== {' '.join(args).replace(PORT, 'PORT')}\n   ({title}, {seconds} s each)")
    width = 34
    results = [(rust, "Rust (" + folder + ")"), (c, "C (" + (c_path or "not installed") + ")")]
    summaries = [gauge_summary(worker, result["frames"]) if result else None for result, _ in results]
    print(f"   {'':15}{results[0][1]:{width}}{results[1][1]}")

    def row(label, show):
        cells = [show(result, summary) if result else "-" for (result, _), summary in zip(results, summaries)]
        print(f"   {label:15}{cells[0]:{width}}{cells[1]}")

    row("exit code", lambda result, _: str(result["code"]))
    row("run time", lambda result, _: f"{result['seconds']:.1f} s")
    row("peak memory", lambda result, _: result["peak"])
    row("frames", lambda _, s: f"{s['frames']}, {s['rate']:.1f} a second")
    row("longest gap", lambda _, s: f"{s['longest']} ms, {s['stalls']} stall{'s' if s['stalls'] != 1 else ''}")
    names = READINGS if worker == "boostdeck-collector" else SPEEDS
    for i, (name, unit_name, _) in enumerate(names):
        def reading(_, s, i=i, unit_name=unit_name):
            share, median = s["readings"][i]
            return "never given" if median is None else f"{median:.1f} {unit_name}".strip() + f", {share:.0%} given"
        row(name, reading)
    row("stderr", lambda result, _: describe(result["stderr"]))
    for result, name in ((rust, "Rust"), (c, "C")):
        if result and result["moved"]:
            print(f"   The {name} worker found the gateway at {result['moved']}, not at the saved address. It "
                  "saved that in its /tmp folder only. setup.sh gateway updates the apps' setting.")
        if result:
            for line in refusals(worker, result["stderr"]):
                print(f"   {name} stderr: {line}")
    if worker == "shift-deck" and all(s and s["readings"][0][1] == 0 for s in summaries if s):
        print("   Engine speed was 0: start the engine for live readings.")
    if c is None:
        print("   Result: no installed C worker to compare with")
        return None
    problems = gauge_problems(worker, rust, c)
    if problems:
        print("   Result: DIFFERENT: " + "; ".join(problems))
        return False
    print(f"   Result: MATCH, the same readings, frame rates within {RATE_TOLERANCE:.0%} and medians within "
          "tolerance")
    return True


def lines(data):
    return data.decode(errors="replace").splitlines()


def describe(data):
    count = len(lines(data))
    return "empty" if not data else f"{count} line{'s' if count != 1 else ''}"


def compare(title, args, rust, c, folder, c_path):
    print(f"\n== {' '.join(args)}\n   ({title})")
    width = 34
    print(f"   {'':15}{'Rust (' + folder + ')':{width}}C ({c_path or 'not installed'})")
    rows = [("exit code", "code", str), ("run time", "seconds", lambda s: f"{s:.2f} s"), ("peak memory", "peak", str),
            ("stdout", "stdout", describe), ("stderr", "stderr", describe)]
    for label, key, show in rows:
        print(f"   {label:15}{show(rust[key]):{width}}{show(c[key]) if c else '-'}")
    for result, name in ((rust, "Rust"), (c, "C")):
        if result and result["moved"]:
            print(f"   The {name} worker found the gateway at {result['moved']}, not at the saved address. It "
                  "saved that in its /tmp folder only. setup.sh gateway updates the apps' setting.")
    if c is None:
        print("   stdout:")
        for line in lines(rust["stdout"]):
            print(f"     {line}")
        print("   Result: no installed C worker to compare with")
        return None
    same = rust["code"] == c["code"] and rust["stdout"] == c["stdout"] and rust["stderr"] == c["stderr"]
    if same:
        print("   stdout, the same from both:")
        for line in lines(rust["stdout"]) or ["(empty)"]:
            print(f"     {line}")
    else:
        print("   stdout, C then Rust:")
        for line in difflib.unified_diff(lines(c["stdout"]), lines(rust["stdout"]), "C", "Rust", lineterm="", n=99):
            print(f"     {line}")
    if rust["stderr"] != c["stderr"]:
        print("   stderr differs. C:")
        for line in lines(c["stderr"]):
            print(f"     {line}")
        print("   Rust:")
        for line in lines(rust["stderr"]):
            print(f"     {line}")
    print("   Result: " + ("MATCH, the same stdout, stderr and exit code" if same else "DIFFERENT"))
    return same


def cleanup(unit, folder):
    """Ends the processes this script started that are still running (a worker, and ena-capture), then deletes the
    folder."""
    if not folder or not FOLDER.match(folder):
        return
    quoted = shlex.quote(folder)
    script = f"""
d={quoted}
if [ -f "$d/pid" ]; then
    while read -r pid started rest; do
        case $pid in ''|*[!0-9]*) continue ;; esac
        now=$(awk '{{print $22}}' "/proc/$pid/stat" 2>/dev/null)
        if [ -n "$started" ] && [ "$now" = "$started" ]; then
            kill -TERM "$pid" 2>/dev/null
            n=0
            while [ -d "/proc/$pid" ] && [ $n -lt 30 ]; do usleep 100000; n=$((n + 1)); done
        fi
    done <"$d/pid"
fi
rm -rf "$d"
[ ! -e "$d" ] && echo CLEANED
"""
    try:
        _, out, _ = unit.text(script, timeout=30)
    except Exception as error:  # noqa: BLE001 - the connection may be gone; say what is left behind
        out = f"({error})"
    if "CLEANED" in out:
        print(f"Deleted {folder} on the head unit")
    else:
        print(f"Could not delete {folder} on the head unit {out.strip()}. /tmp empties when the head unit restarts, "
              f"or delete it by hand: rm -rf {folder}")


def interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Runs the Rust workers on a car beside the installed C ones and compares them. Read-only; installs "
                    "nothing. Every argument that writes to the engine computer is refused: engine-codes --clear, "
                    "bm3-maps --map, --burbles and --sound, and mhd-maps --map and --cooling. bm3-maps runs only "
                    "--status, and mhd-maps only a run without an action, which sends nothing and is left out when "
                    "MHD+ Suite isn't installed.")
    parser.add_argument("--host", required=True, help="the head unit's address on the car's Wi-Fi or ENET")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--worker", choices=("all", "gauges", *WORKERS), default="all",
                        help="all six workers, both gauge workers, or one")
    parser.add_argument("--worker-args", help="read-only arguments for --worker instead of the apps' own, quoted as "
                                              "one string, for example \"check --host 169.254.10.3\" or "
                                              "\"--host 169.254.10.3 --status --dump\" for bm3-maps")
    parser.add_argument("--seconds", type=int, default=20, choices=range(3, 301), metavar="3-300",
                        help="how long each gauge worker runs (default 20)")
    parser.add_argument("--rust-dir", type=Path, default=ROOT / "build" / "rust", help="where make rust put them")
    parser.add_argument("--timeout", type=int, default=60, help="seconds a one-shot run may take")
    options = parser.parse_args(argv)
    workers = {"all": list(WORKERS), "gauges": list(GAUGES)}.get(options.worker, [options.worker])
    custom = None
    if options.worker_args is not None:
        if len(workers) > 1:
            parser.error("--worker-args needs --worker and one worker's name")
        custom = shlex.split(options.worker_args)
        why = refused(options.worker, custom)
        if why:
            print(f"Refused: {why}", file=sys.stderr)
            return EXIT_REFUSED
    binaries = {}
    for worker in workers + ([CAPTURE] if set(workers) & set(GAUGES) else []):
        path = options.rust_dir / worker
        if not path.is_file():
            print(f"{path} is missing. Build it first: make rust", file=sys.stderr)
            return EXIT_STOPPED
        binaries[worker] = path.read_bytes()
        if binaries[worker][:7] != b"\x7fELF\x01\x01\x01" or binaries[worker][18] != 40:
            print(f"{path} isn't a 32-bit ARM program", file=sys.stderr)
            return EXIT_STOPPED
    password = os.environ.get("HEADUNIT_PASSWORD") or getpass.getpass(f"root password for {options.host}: ")
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupt)  # Ctrl-Break on Windows stops it as Ctrl-C does
    unit, folder, status = None, None, 0
    try:
        unit = HeadUnit(options.host, options.ssh_port, password)
        del password
        # The uploads, read back once, and a gauge run's frames: at most about 30 kB a second.
        need_kb = sum(len(data) for data in binaries.values()) // 1024 * 2 + 256
        if CAPTURE in binaries:
            need_kb += options.seconds * 30
        installed, openssl = preflight(unit, workers, need_kb)
        for worker, why in NEEDS_APP.items():
            if worker in workers and installed[worker] is None:
                print(why)
                workers.remove(worker)
                del binaries[worker]
        gateway = saved_gateway(unit)
        jobs = [(worker, title, args) for worker in workers for title, args in JOBS[worker]]
        if custom is not None:
            jobs = [(workers[0], "the arguments given", custom)] if workers else []
        if not jobs:
            print("\nNothing to compare.")
            return 0
        if any(GATEWAY in args for _, _, args in jobs) and not gateway:
            raise Stop(f"No usable gateway address in {GATEWAY_FILE}, so the apps can't reach the car (Engine Fault "
                       "Codes says \"Gateway not set up\"). sh /opt/hmi/data/ro/Plugins/EngineCodes/setup.sh gateway "
                       "finds it.")
        if gateway:
            print(f"Saved gateway: {gateway}")
        folder = make_folder(unit)
        print(f"Made {folder}")
        for name, data in binaries.items():
            upload(unit, folder, name, data, openssl)
        results = []
        for worker, title, args in jobs:
            args = [gateway if a == GATEWAY else a for a in args]
            if worker in GAUGES:  # its frames to ena-capture, never to the app's port
                args += ["--udp-port", PORT]
            if worker != "ena-gateway":  # each worker that can search for the gateway saves what it finds here
                args += ["--gateway-file", f"{folder}/moved-gateway.txt"]
            outcome = {}
            for build, program in (("rust", f"{folder}/{worker}"), ("c", installed[worker])):
                if program is None:
                    outcome[build] = None
                    continue
                why = busy(unit)
                if why:
                    raise Stop(why + " An app's page opened in iDrive while this ran. Close it and run this again.")
                if worker in GAUGES:
                    label = "Rust" if build == "rust" else "C"
                    print(f"Running the {label} {worker} for {options.seconds} s...", flush=True)
                    outcome[build] = run_gauge(unit, folder, program, args, options.seconds)
                else:
                    outcome[build] = run_worker(unit, folder, program, args, options.timeout)
                if LEASE_HELD.encode() in outcome[build]["stdout"]:
                    raise Stop(f"The {build.upper()} worker found the diagnostics connection busy: an app took it "
                               "during the run. Close its page in iDrive and run this again.")
                time.sleep(0.3)
            if worker in GAUGES:
                results.append(compare_gauges(worker, title, args, outcome["rust"], outcome["c"], folder,
                                              installed[worker], options.seconds))
            else:
                results.append(compare(title, args, outcome["rust"], outcome["c"], folder, installed[worker]))
        compared = [r for r in results if r is not None]
        print(f"\n{sum(compared)} of {len(compared)} compared runs matched." if compared else "\nNothing to compare.")
        status = 0 if all(compared) else EXIT_MISMATCH
    except Stop as stop:
        print(f"Stopped: {stop}", file=sys.stderr)
        status = EXIT_STOPPED
    except paramiko.AuthenticationException:
        print("The head unit refused the password.", file=sys.stderr)
        status = EXIT_STOPPED
    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up on the head unit...", file=sys.stderr)
        status = EXIT_INTERRUPTED
    except TimeoutError:
        print("The head unit didn't answer in time.", file=sys.stderr)
        status = EXIT_STOPPED
    except (OSError, paramiko.SSHException) as error:
        print(f"Lost the head unit: {error}", file=sys.stderr)
        status = EXIT_STOPPED
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # a second Ctrl-C must not skip the cleanup
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        if unit is not None:
            cleanup(unit, folder)
            unit.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
