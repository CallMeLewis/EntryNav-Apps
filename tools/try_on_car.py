#!/usr/bin/env python3
"""Tries the Rust ports of ena-gateway and engine-codes on a car, from a laptop, beside the installed C workers. It
installs nothing and needs no Flare Drive.

    uv run --no-project --with paramiko tools/try_on_car.py --host HEAD_UNIT_ADDRESS [--worker all]

It logs in over SSH as root, with the password from HEADUNIT_PASSWORD or a prompt. The password is never printed or
saved. Then it:
1. reads the firmware line of /etc/version, and checks that /tmp is writable, lets programs run and has room. It stops
   if an EntryNav-Apps worker holds the diagnostic lease (UDP 127.0.0.1:42861) or is running, because then an app's
   page is open in iDrive and the two would take turns on the gateway;
2. makes a folder with mktemp -d /tmp/ena-rust.XXXXXX, uploads build/rust's workers into it (make rust builds them)
   and checks each one's SHA-256 on the car;
3. runs each read-only job the apps give these workers, the Rust worker first and then the installed C one, with the
   same arguments: engine-codes --host GATEWAY --codes, as Engine Fault Codes runs it, and ena-gateway check --host
   GATEWAY and ena-gateway discover, as setup.sh gateway runs them. engine-codes also gets --gateway-file in the
   folder, so that a worker that finds the gateway at a new address saves it there instead of in the apps' settings;
4. prints both results side by side, with exit code, run time and peak memory, and whether they match;
5. deletes its folder, also after an error or Ctrl-C.

It never writes outside its folder, never changes /opt, restarts nothing, and refuses --clear and every argument that
isn't one of the workers' read-only ones. --worker-args runs one worker with other read-only arguments.
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
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ("engine-codes", "ena-gateway")
PLUGINS = "/opt/hmi/data/ro/Plugins"
# Where each app has its C worker (src/paths.h), and the plugin folders setup.sh puts ena-gateway in.
INSTALLED = {"engine-codes": [f"{PLUGINS}/EngineCodes/engine-codes"],
             "ena-gateway": [f"{PLUGINS}/{app}/ena-gateway"
                             for app in ("EngineCodes", "BoostDeck", "ShiftDeck", "Bm3Maps", "MhdMaps")]}
GATEWAY_FILE = "/var/local/data/EntryNavApps/gateway-ip.txt"
GAUGE_WORKERS = ("boostdeck-collector", "shift-deck", "engine-codes", "bm3-maps", "mhd-maps")
FOLDER = re.compile(r"^/tmp/ena-rust\.[A-Za-z0-9]{6}$")
GATEWAY = "GATEWAY"  # replaced by the saved gateway address
# The jobs the apps give each worker: what it does, and its arguments.
JOBS = {"engine-codes": [("read the engine's fault codes, as Engine Fault Codes does", ["--host", GATEWAY, "--codes"])],
        "ena-gateway": [("check the route to the gateway, as setup.sh gateway does", ["check", "--host", GATEWAY]),
                        ("look for the gateway on vbr0, as setup.sh gateway does", ["discover"])]}
LEASE_HELD = "Diagnostics busy"
EXIT_MISMATCH, EXIT_REFUSED, EXIT_STOPPED, EXIT_INTERRUPTED = 1, 2, 3, 130


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
    once."""
    if "--clear" in args:
        return "--clear clears the engine computer's fault memory. This script only reads."
    if worker == "engine-codes":
        options = {"--host": address, "--port": lambda v: number(v, 1, 65535)}
        flags, rest = {"--codes"}, list(args)
        if "--codes" not in rest:
            return "engine-codes needs --codes, its read-only job"
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
            elif channel.exit_status_ready():
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
    code, _, err = unit.text(shlex.quote(target))  # no arguments: it prints its usage and exits 2
    if code != 2 or not err.startswith("Usage:"):
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
    return {"code": int(found["CODE"][0]), "stdout": stdout, "stderr": stderr, "seconds": end - start,
            "peak": f"{peak} kB" if peak.split() and peak.split()[0].isdigit() else "not readable",
            "moved": (found.get("MOVED") or [None])[0]}


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
    same = rust["code"] == c["code"] and rust["stdout"] == c["stdout"]
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
    print("   Result: " + ("MATCH, same stdout and exit code" if same else "DIFFERENT"))
    return same


def cleanup(unit, folder):
    """Ends a worker this script started that is still running, then deletes the folder."""
    if not folder or not FOLDER.match(folder):
        return
    quoted = shlex.quote(folder)
    script = f"""
d={quoted}
if [ -f "$d/pid" ]; then
    read -r pid started rest <"$d/pid"
    case $pid in ''|*[!0-9]*) pid= ;; esac
    now=$(awk '{{print $22}}' "/proc/$pid/stat" 2>/dev/null)
    if [ -n "$pid" ] && [ -n "$started" ] && [ "$now" = "$started" ]; then
        kill -TERM "$pid" 2>/dev/null
        n=0
        while [ -d "/proc/$pid" ] && [ $n -lt 30 ]; do usleep 100000; n=$((n + 1)); done
    fi
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
    parser = argparse.ArgumentParser(description="Runs the Rust workers on a car beside the installed C ones and "
                                                 "compares them. Read-only; installs nothing.")
    parser.add_argument("--host", required=True, help="the head unit's address on the car's Wi-Fi or ENET")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--worker", choices=("all", *WORKERS), default="all")
    parser.add_argument("--worker-args", help="read-only arguments for --worker instead of the apps' own, quoted as "
                                              "one string, for example \"check --host 169.254.10.3\"")
    parser.add_argument("--rust-dir", type=Path, default=ROOT / "build" / "rust", help="where make rust put them")
    parser.add_argument("--timeout", type=int, default=60, help="seconds a run may take")
    options = parser.parse_args(argv)
    workers = list(WORKERS) if options.worker == "all" else [options.worker]
    custom = None
    if options.worker_args is not None:
        if options.worker == "all":
            parser.error("--worker-args needs --worker engine-codes or --worker ena-gateway")
        custom = shlex.split(options.worker_args)
        why = refused(options.worker, custom)
        if why:
            print(f"Refused: {why}", file=sys.stderr)
            return EXIT_REFUSED
    binaries = {}
    for worker in workers:
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
        need_kb = sum(len(data) for data in binaries.values()) // 1024 * 2 + 256
        installed, openssl = preflight(unit, workers, need_kb)
        gateway = saved_gateway(unit)
        jobs = [(worker, title, args) for worker in workers for title, args in JOBS[worker]]
        if custom is not None:
            jobs = [(workers[0], "the arguments given", custom)]
        if any(GATEWAY in args for _, _, args in jobs) and not gateway:
            raise Stop(f"No usable gateway address in {GATEWAY_FILE}, so Engine Fault Codes would say "
                       "\"Gateway not set up\". sh /opt/hmi/data/ro/Plugins/EngineCodes/setup.sh gateway finds it.")
        if gateway:
            print(f"Saved gateway: {gateway}")
        folder = make_folder(unit)
        print(f"Made {folder}")
        for worker in workers:
            upload(unit, folder, worker, binaries[worker], openssl)
        results = []
        for worker, title, args in jobs:
            args = [gateway if a == GATEWAY else a for a in args]
            if worker == "engine-codes":
                args += ["--gateway-file", f"{folder}/moved-gateway.txt"]
            outcome = {}
            for build, program in (("rust", f"{folder}/{worker}"), ("c", installed[worker])):
                if program is None:
                    outcome[build] = None
                    continue
                why = busy(unit)
                if why:
                    raise Stop(why + " An app's page opened in iDrive while this ran. Close it and run this again.")
                outcome[build] = run_worker(unit, folder, program, args, options.timeout)
                if LEASE_HELD.encode() in outcome[build]["stdout"]:
                    raise Stop(f"The {build.upper()} worker found the diagnostics connection busy: an app took it "
                               "during the run. Close its page in iDrive and run this again.")
                time.sleep(0.3)
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
        print(f"The head unit didn't answer within {options.timeout} s.", file=sys.stderr)
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
