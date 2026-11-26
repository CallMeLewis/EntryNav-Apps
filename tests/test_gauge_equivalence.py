r"""The C and Rust builds of the gauge workers, boostdeck-collector and shift-deck, side by side. Each scenario runs
every build in ENA_WORKERS against the same pretend gateway, with a pretend app listening for the frames, and compares
each build with the first, the C, or with what the C did as tests/fixtures records it (tests/reference.py).

What can't depend on timing must be the same: stdout, stderr (the trace's clock readings aside), the exit code, the
frames' format, each reading's sequence of values as the app sees it, the gateway's first requests and which ECU each
DID goes to, the lease's waiting bytes, files written, and whether the worker stops when its parent goes. What depends
on timing (how many frames and requests in a stream, the gaps between them, when the slow readings come, how long a run
or a wait takes) must agree within the tolerance each scenario gives, since QEMU's speed varies. Scenarios: the first
pass, streaming with changing values, the slow readings' schedule, refusals and bad payloads, acknowledgements, pending
answers and alive checks, oversized and short answers, a gateway that goes silent or closes the connection, the lease
held, given up or asked for by another worker, SIGTERM, the parent going, a gateway that moved, demo mode, and wrong
arguments.

Needs two builds, as tests/run_rust.py runs it:
    ENA_WORKERS=c=build-arm,rust=build/rust ENA_WORKER_RUNNER="qemu-arm -L /sysroot ..." \
        python3 tests/test_gauge_equivalence.py
"""

import errno
import os
import re
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import reference
from test_transport import CAR_VALUES, VALUES, frame
from test_worker_equivalence import CRASHED, LEASE_PORT, Lease, Responder, add_tests, identification, pairs, run
from workers import builds

BOOST_DIDS = (0x4201, 0x4205, 0x5889, 0x4A49, 0x580F, 0xDA12, 0x56D7, 0x586F, 0x4300)
SLOW_DIDS = {0x4201: 1000, 0x580F: 2000, 0xDA12: 2000, 0x4300: 2000}  # and their periods
BOOST_TABLE = {f"22{did:04x}": [f"62{did:04x}{payload.hex()}"] for did, payload in VALUES.items()}
CAR_TABLE = {f"22{did:04x}": [f"62{did:04x}{payload.hex()}"] for did, payload in CAR_VALUES.items()}
SHIFT_TABLE = {"010c": ["410c0ae0"], "010d": ["410d00"]}  # 696 rpm, standing


def boost_values(**dids):
    """BOOST_TABLE with other answers for some DIDs: did4205=["31e5", "3300"] answers 4205 with each payload in turn.
    A negative answer (7F...), a token or a tuple of answers is sent as it is (GaugeGateway)."""
    table = dict(BOOST_TABLE)
    for name, payloads in dids.items():
        did = name[3:]
        table[f"22{did}"] = [p if isinstance(p, tuple) or p in ("close", "silent", "hang", "oversized")
                             or p.startswith("7f") else f"62{did}{p}" for p in payloads]
    return table


# ---- the pretend gateway ---------------------------------------------------------------------------------------------

class GaugeGateway:
    """HSFZ on 127.0.0.1 for a gauge worker, one connection at a time. table maps a request's UDS hex to its answers,
    used one per request in turn, the last repeating. An answer is the reply's UDS hex (from the ECU the request went
    to), or a tuple of such answers and these tokens sent one after the other: "ack" (the gateway's acknowledgement),
    "alive" (an alive check), "oversized" (a frame header of 500,000 bytes); or "close" (this connection), "silent" (no
    answer to this request) or "hang" (no more answers on this connection). Requests from number silent_from on go
    unanswered, and close_every closes each connection at its that-many-th request. Unknown requests get NRC 11.

    It records each request as (seconds from the first, connection, ECU, UDS hex), and every frame that isn't a
    request."""

    def __init__(self, table, silent_from=None, close_every=None):
        self.table = {request: list(answers) for request, answers in table.items()}
        self.silent_from, self.close_every = silent_from, close_every
        self.requests, self.others, self.connections = [], [], 0
        self.first = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.done.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            self.connections += 1
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(0.2)
                try:
                    self.talk(conn)
                except (OSError, EOFError):
                    pass

    def receive(self, conn, n):
        data = b""
        while len(data) < n:
            try:
                more = conn.recv(n - len(data))
            except TimeoutError:
                if self.done.is_set():
                    raise EOFError from None
                continue
            if not more:
                raise EOFError
            data += more
        return data

    def talk(self, conn):
        hung, on_this = False, 0
        while True:
            size, control = struct.unpack(">IH", self.receive(conn, 6))
            body = self.receive(conn, size)
            if control != 1:
                self.others.append((control, body.hex()))
                continue
            now = time.monotonic()
            self.first = self.first or now
            ecu, uds = body[1], body[2:]
            number = len(self.requests)
            self.requests.append((now - self.first, self.connections, ecu, uds.hex()))
            on_this += 1
            if self.close_every and on_this >= self.close_every:
                return
            if hung or (self.silent_from is not None and number >= self.silent_from):
                continue
            answers = self.table.get(uds.hex())
            answer = (answers.pop(0) if len(answers) > 1 else answers[0]) if answers else f"7f{uds[0]:02x}11"
            if answer == "close":
                return
            if answer == "silent":
                continue
            if answer == "hang":
                hung = True
                continue
            for part in answer if isinstance(answer, tuple) else (answer,):
                if part == "ack":
                    raw = frame(2, bytes([0xF4, ecu]) + uds)
                elif part == "alive":
                    raw = frame(0x12, b"\0\xf4")
                elif part == "oversized":
                    raw = struct.pack(">IH", 500000, 1)
                else:
                    raw = frame(1, bytes([ecu, 0xF4]) + bytes.fromhex(part))
                # In two pieces, so that a worker reading a frame in one go is caught.
                conn.sendall(raw[:3])
                conn.sendall(raw[3:])

    def stop(self):
        self.done.set()
        self.thread.join(5)
        self.listener.close()


class App:
    """The app's side: UDP on 127.0.0.1, recording each datagram with the moment it arrived."""

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.datagrams = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def listen(self):
        while not self.done.is_set():
            try:
                data = self.socket.recv(4096)
            except TimeoutError:
                continue
            self.datagrams.append((time.monotonic(), data))

    def stop(self):
        time.sleep(0.1)  # what is still on its way
        self.done.set()
        self.thread.join(5)
        self.socket.close()


class Waiter:
    """A one-shot worker that wants the gateway start_after seconds in: every 25 ms it tries for the lease and, while it
    is held, sends the holder the waiting byte (reserve_gateway). Once it has the lease it holds it for hold seconds."""

    def __init__(self, start_after, hold=0.3):
        self.start_after, self.hold, self.waited = start_after, hold, None
        self.thread = threading.Thread(target=self.wait, daemon=True)
        self.thread.start()

    def wait(self):
        time.sleep(self.start_after)
        started = time.monotonic()
        while time.monotonic() - started < 5:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as lock:
                try:
                    lock.bind(("127.0.0.1", LEASE_PORT))
                except OSError as error:
                    if error.errno != errno.EADDRINUSE:
                        raise
                    lock.sendto(b"\x01", ("127.0.0.1", LEASE_PORT))
                else:
                    self.waited = time.monotonic() - started
                    time.sleep(self.hold)
                    return
            time.sleep(0.025)

    def stop(self):
        self.thread.join(8)


def lease_free():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind(("127.0.0.1", LEASE_PORT))
            return True
        except OSError:
            return False


def gone(pid):
    """Whether pid has ended: no longer there, or a zombie nobody waits for."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] in "ZX"
    except (FileNotFoundError, IndexError):
        return True


# ---- what is compared --------------------------------------------------------------------------------------------

class Within:
    """A measurement that must agree with the reference's within absolute or relative tolerance, whichever is larger."""

    def __init__(self, value, absolute, relative=0.0):
        self.value, self.absolute, self.relative = value, absolute, relative

    def agrees(self, reference):
        if self.value is None or reference.value is None:
            return self.value is reference.value
        return abs(self.value - reference.value) <= max(self.absolute, self.relative * abs(reference.value))

    def __repr__(self):
        return "None" if self.value is None else f"{self.value:.3f}"


def collapse(items):
    """items without consecutive repeats."""
    out = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def intervals(times):
    return [b - a for a, b in zip(times, times[1:])]


def median_ms(times):
    gaps = intervals(times)
    return statistics.median(gaps) * 1000 if gaps else None


def on_the_monotonic_clock(arrived, stamp_ms):
    """Whether a frame's stamp is the system's CLOCK_MONOTONIC, which Python's time.monotonic() reads on Linux: the
    frame arrived within half a second of it."""
    return abs(arrived * 1000 - stamp_ms) < 500


def boost_frames(datagrams, slack, loose=False):
    """What the BoostDeck app would see in the datagrams: exact parts, and measurements within slack. Each reading is
    its sequence of (valid, value, ever read) as the frames change it. loose, for scenarios where a failure lands on a
    pass that timing decides, keeps instead the sequence of values read and the set of (valid, ever read) seen."""
    formats, seqs, demo, clock, valid_times = set(), [], set(), True, []
    readings = [[] for _ in range(8)]
    for arrived, data in datagrams:
        text = data.decode(errors="replace")
        fields = text.split()
        formats.add((fields[0] if fields else "", len(fields), text.endswith("\n") and text.count("\n") == 1))
        if len(fields) != 21 or fields[0] != "NBG3":
            continue
        seqs.append(int(fields[1]))
        demo.add(fields[2])
        valid, sent = int(fields[3]), int(fields[4])
        clock &= on_the_monotonic_clock(arrived, sent)
        if valid:
            valid_times.append(arrived)
        for i in range(8):
            value, age = fields[5 + 2 * i], int(fields[6 + 2 * i])
            # A reading never read is as old as the clock (at most 999999): only whether it was read is compared.
            seen = age != min(sent, 999999)
            readings[i].append((valid >> i & 1, value, seen))
    times = [t for t, _ in datagrams]
    if loose:
        readings = [{"values read": collapse(value for valid, value, _ in r if valid),
                     "states": sorted({(valid, seen) for valid, _, seen in r})} for r in readings]
    else:
        readings = [collapse(r) for r in readings]
    return {"formats": sorted(formats), "seq counts from 1": seqs == list(range(1, len(seqs) + 1)),
            "demo": sorted(demo), "monotonic clock": clock, "readings": readings,
            "timing": {"frames": Within(len(datagrams), *slack["frames"]),
                       "frames with a reading": Within(len(valid_times), *slack["frames"]),
                       "median gap, ms": Within(median_ms(times), 6, 0.25)}}


def shift_frames(datagrams, slack):
    """What the ShiftDeck app would see."""
    sizes, rpm, kph, clock, stamps = set(), [], [], True, []
    for arrived, data in datagrams:
        sizes.add(len(data))
        if len(data) != 16:
            continue
        sampled, r, k = struct.unpack("<Qii", data)
        clock &= on_the_monotonic_clock(arrived, sampled)
        stamps.append(sampled)
        rpm.append(r)
        kph.append(k)
    times = [t for t, _ in datagrams]
    return {"sizes": sorted(sizes), "rpm": collapse(rpm), "kph": collapse(kph), "monotonic clock": clock,
            "stamps in order": stamps == sorted(stamps),
            "timing": {"frames": Within(len(datagrams), *slack["frames"]),
                       "median gap, ms": Within(median_ms(times), 6, 0.25)}}


def requests_summary(gateway, first, slack):
    """The gateway's side: the first requests exactly, which ECU each request went to, the frames that weren't
    requests, and per request how many and how they were spaced."""
    by_request = {}
    for at, _, ecu, uds in gateway.requests:
        by_request.setdefault((ecu, uds), []).append(at)
    timing = {"connections": Within(gateway.connections, *slack["connections"])}
    for (ecu, uds), times in sorted(by_request.items()):
        timing[f"{ecu:02X} {uds}: requests"] = Within(len(times), *slack["requests"])
        timing[f"{ecu:02X} {uds}: first, s"] = Within(times[0], slack["seconds"])
        if len(times) > 1:
            timing[f"{ecu:02X} {uds}: median gap, ms"] = Within(median_ms(times), 10, 0.15)
    return {"first requests": [(ecu, uds) for _, _, ecu, uds in gateway.requests[:first]],
            "requests": sorted(by_request), "other frames": collapse(gateway.others),
            # UDS ReadDataByIdentifier (22) and OBD current data (01): nothing that writes to an ECU.
            "only reads": all(uds[:2] in ("22", "01") for _, _, _, uds in gateway.requests), "timing": timing}


# Slack for the measurements of a stream: (absolute, relative) for counts, seconds for times.
STEADY = {"frames": (0, 0), "connections": (0, 0), "requests": (0, 0), "seconds": 0.15}
STREAM = {"frames": (3, 0.12), "connections": (1, 0.15), "requests": (3, 0.12), "seconds": 0.15}


def frames_of(worker_name, datagrams, slack, loose=False):
    if worker_name == "boostdeck-collector":
        return boost_frames(datagrams, slack, loose)
    return shift_frames(datagrams, slack)


def stream(worker_name, table, *args, slack=STEADY, first=9, term_after=None, lease=None, waiter=None,
           silent_from=None, close_every=None, loose=False, host="127.0.0.1", timeout=40):
    """A scenario: the worker streams to the app from the gateway, until --cycles, SIGTERM or the timeout."""
    def scenario(worker):
        gateway = GaugeGateway(table, silent_from, close_every)
        app = App()
        holder = Lease(**lease) if lease is not None else None
        asker = Waiter(**waiter) if waiter is not None else None
        try:
            result = run(worker, ["--host", host, "--port", str(gateway.port), "--udp-port", str(app.port), *args],
                         term_after, timeout)
        finally:
            gateway.stop()
            app.stop()
            if asker:
                asker.stop()
            if holder:
                holder.stop()
        result["stderr"] = re.sub(r"mono_ms=\d+", "mono_ms=T", result["stderr"])
        seconds = result.pop("seconds")
        frames = frames_of(worker_name, app.datagrams, slack, loose)
        requests = requests_summary(gateway, first, slack)
        result.update({key: value for key, value in frames.items() if key != "timing"})
        result.update({key: value for key, value in requests.items() if key != "timing"})
        result["timing"] = {**frames["timing"], **requests["timing"], "run, s": Within(seconds, 0.5, 0.15)}
        result["lease free after"] = lease_free()
        if holder and holder.timer:  # one byte a pass until it lets go: as many as the passes that fit in the wait
            result["timing"]["waiting bytes"] = Within(holder.asked, 2, 0.25)
        elif holder:  # held throughout: one byte every pass
            result["waiting bytes"] = holder.asked
        if asker:
            result["the other worker got the lease"] = asker.waited is not None
            result["timing"]["the other worker waited, s"] = Within(asker.waited, 0.15)
        return result
    return scenario


def orphaned(worker_name, *args, parent_for=0.6):
    """A scenario: the worker's parent leaves after parent_for seconds. In a session the worker stops within a pass;
    without one it carries on to its --cycles."""
    def scenario(worker):
        gateway = GaugeGateway(BOOST_TABLE if worker_name == "boostdeck-collector" else SHIFT_TABLE)
        app = App()
        parent = subprocess.Popen(
            [sys.executable, "-W", "ignore", "-c", "import subprocess, sys, time\n"
             "child = subprocess.Popen(sys.argv[2:])\n"
             "print(child.pid, flush=True)\n"
             "time.sleep(float(sys.argv[1]))\n",
             str(parent_for), *worker, "--host", "127.0.0.1", "--port", str(gateway.port), "--udp-port",
             str(app.port), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid = int(parent.stdout.readline())
        parent.wait(10)
        left = time.monotonic()
        while not gone(pid) and time.monotonic() - left < 10:
            time.sleep(0.01)
        stopped_after = time.monotonic() - left
        err = parent.stderr.read().decode(errors="replace").replace(worker[-1], "WORKER")
        gateway.stop()
        app.stop()
        frames = frames_of(worker_name, app.datagrams, STREAM)
        return {"stderr": err, "stopped within 0.5 s of its parent": stopped_after < 0.5,
                "streamed before": bool(app.datagrams), "lease free after": lease_free(),
                "timing": {"stopped after, s": Within(stopped_after, 0.3)},
                "readings": frames.get("readings", frames.get("rpm"))}
    return scenario


def moved(worker_name, *args):
    """A scenario: nothing answers at the saved address. After three failures the worker asks lo, finds the gateway on
    127.0.0.1, saves it in --gateway-file and streams from it."""
    def scenario(worker):
        responder = Responder([identification()])
        with tempfile.TemporaryDirectory() as data:
            saved = Path(data) / "gateway-ip.txt"
            gateway = GaugeGateway(BOOST_TABLE if worker_name == "boostdeck-collector" else SHIFT_TABLE)
            app = App()
            try:
                result = run(worker, ["--host", "127.0.0.2", "--port", str(gateway.port), "--udp-port", str(app.port),
                                      "--search-dev", "lo", "--search-port", str(responder.port), "--gateway-file",
                                      str(saved), *args])
            finally:
                gateway.stop()
                app.stop()
            result["saved"] = saved.read_text() if saved.exists() else None
            result["left behind"] = sorted(p.name for p in Path(data).iterdir())
        responder.stop()
        result["identification requests"] = responder.received
        frames = frames_of(worker_name, app.datagrams, STREAM)
        seconds = result.pop("seconds")
        result.update({key: value for key, value in frames.items() if key != "timing"})
        result["timing"] = {**frames["timing"], "run, s": Within(seconds, 0.5, 0.15),
                            "connections": Within(gateway.connections, *STREAM["connections"])}
        return result
    return scenario


def demo(worker):
    """BoostDeck's demo: made-up readings, no gateway."""
    app = App()
    try:
        result = run(worker, ["--demo", "--cycles", "5", "--udp-port", str(app.port)])
    finally:
        app.stop()
    frames = boost_frames(app.datagrams, STEADY)
    result.update({key: value for key, value in frames.items() if key != "timing"})
    result["timing"] = {**frames["timing"], "run, s": Within(result.pop("seconds"), 0.5)}
    return result


def arguments_only(*args):
    def scenario(worker):
        result = run(worker, list(args))
        result["timing"] = {"run, s": Within(result.pop("seconds"), 0.5)}
        return result
    return scenario


B, S = "boostdeck-collector", "shift-deck"
SESSION = ("--session",)
RPM = [f"410c{4 * rpm:04x}" for rpm in range(700, 6700, 150)]  # 40 readings, rising

BOOSTDECK = {
    "one pass: every reading": stream(B, BOOST_TABLE, "--cycles", "1"),
    "one pass, traced: the car's snapshot": stream(B, CAR_TABLE, "--cycles", "1", "--trace"),
    "one pass in a session": stream(B, BOOST_TABLE, "--cycles", "1", *SESSION),
    "a session streams: boost changes, slow readings staggered": stream(
        B, boost_values(did4205=[f"{128 * kpa:04x}" for kpa in range(100, 140)], did580f=["82", "84"]),
        "--cycles", "100", *SESSION, slack=STREAM),
    "streaming without a session keeps its connection": stream(
        B, boost_values(did4205=[f"{128 * kpa:04x}" for kpa in range(100, 130)]), "--cycles", "40",
        slack={**STREAM, "connections": (0, 0)}),
    "streaming without the EGS": stream(B, BOOST_TABLE, "--cycles", "30", "--no-trans", *SESSION, slack=STREAM,
                                        first=8),
    "refusals, sentinels and bad payloads, traced": stream(
        B, boost_values(did4205=["7f2231", "31", "31e5aa", "31e5"], did5889=["104eaa", "104e"],
                        did580f=["ff"], did4a49=["ffff", "fffe"], dida12=["7f2222"], did56d7=["ffff"]),
        "--cycles", "4", "--trace"),
    "acknowledgements, pending answers and alive checks": stream(
        B, boost_values(did4205=[("ack", "7f2278", "alive", "62420531e5")]), "--cycles", "3"),
    "an oversized frame for boost, in a session": stream(B, boost_values(did4205=["oversized"]), "--cycles", "60",
                                                         *SESSION, slack=STREAM, loose=True),
    "an oversized frame without a session": stream(B, boost_values(did4205=["31e5", "oversized", "31e5"]),
                                                   "--cycles", "4"),
    "the longest answer HSFZ allows": stream(B, boost_values(did4205=["31e5" + "00" * 4089]), "--cycles", "2"),
    "an answer one byte over HSFZ's limit": stream(B, boost_values(did4205=["31e5" + "00" * 4090, "31e5"]),
                                                   "--cycles", "2"),
    "the gateway goes silent in a session": stream(B, BOOST_TABLE, "--cycles", "30", *SESSION, silent_from=20,
                                                   slack=STREAM, loose=True),
    "the gateway goes silent without a session": stream(B, BOOST_TABLE, "--cycles", "4", silent_from=12),
    "the gateway closes every seventh request in a session": stream(B, BOOST_TABLE, "--cycles", "60", *SESSION,
                                                                    close_every=7, slack=STREAM, loose=True),
    "the gateway closes without a session": stream(B, BOOST_TABLE, "--cycles", "20", close_every=12, slack=STREAM,
                                                   loose=True),
    "the lease held throughout": stream(B, BOOST_TABLE, "--cycles", "20", *SESSION, lease={}),
    "the lease given up after 0.4 s": stream(B, BOOST_TABLE, "--cycles", "40", *SESSION,
                                             lease={"release_after": 0.4}, slack=STREAM),
    "another worker asks for the lease": stream(B, BOOST_TABLE, "--cycles", "60", *SESSION,
                                                waiter={"start_after": 0.8}, slack=STREAM),
    "SIGTERM part way": stream(B, BOOST_TABLE, *SESSION, term_after=1.0, slack=STREAM),
    "SIGTERM while a read waits": stream(B, BOOST_TABLE, *SESSION, term_after=0.8, silent_from=12, slack=STREAM,
                                         loose=True),
    "its parent goes in a session": orphaned(B, *SESSION),
    "its parent goes without a session": orphaned(B, "--cycles", "40", parent_for=0.3),
    "the gateway moved and is saved": moved(B, "--cycles", "50", *SESSION),
    "no gateway, nothing to search": stream(B, BOOST_TABLE, "--cycles", "6", "--search-dev", "nonexistent0",
                                            host="127.0.0.2"),
    "two hosts: the last counts": stream(B, BOOST_TABLE, "--host", "127.0.0.2", "--cycles", "1"),
    "a host that isn't an address": stream(B, BOOST_TABLE, "--cycles", "2", host="1.2.3"),
    "demo": demo,
    "no arguments": arguments_only(),
    "demo and a host": arguments_only("--demo", "--host", "127.0.0.1"),
    "host without a value": arguments_only("--host"),
    "unknown option": arguments_only("--host", "127.0.0.1", "--verbose"),
    "a stray argument": arguments_only("--host", "127.0.0.1", "--cycles", "1", "extra"),
    "cycles 0": arguments_only("--host", "127.0.0.1", "--cycles", "0"),
    "cycles 1000001": arguments_only("--host", "127.0.0.1", "--cycles", "1000001"),
    "cycles with letters": arguments_only("--cycles", "1O", "--host", "127.0.0.1"),
    "port 65536": arguments_only("--host", "127.0.0.1", "--port", "65536"),
    "UDP port 0": arguments_only("--host", "127.0.0.1", "--udp-port", "0"),
    "search port 0": arguments_only("--host", "127.0.0.1", "--search-port", "0"),
    "search device without a value": arguments_only("--host", "127.0.0.1", "--search-dev"),
    "host too long": arguments_only("--host", "169.254.100.1000", "--cycles", "1"),
    "demo in a session": arguments_only("--demo", "--session", "--cycles", "1"),
}

SHIFTDECK = {
    "one pass": stream(S, SHIFT_TABLE, "--cycles", "1", first=2),
    "streaming: rising revs, speed every 65 ms": stream(
        S, {"010c": RPM, "010d": ["410d00", "410d01", "410d02", "410d03", "410d05"]}, "--cycles", "150", first=2,
        slack=STREAM),
    "refused and short answers": stream(S, {"010c": ["7f0112", "410c0a", "410c0ae0"], "010d": ["410d", "7f0131"]},
                                        "--cycles", "3", first=6),
    "an answer too long to take": stream(S, {"010c": ["410c0ae0ff", "410c0ae0"], "010d": ["410d00"]},
                                         "--cycles", "30", first=2, slack=STREAM),
    "an oversized frame": stream(S, {"010c": ["oversized", "410c0ae0"], "010d": ["410d00"]}, "--cycles", "30",
                                 first=2, slack=STREAM),
    "acknowledgements, pending answers and alive checks": stream(
        S, {"010c": [("ack", "7f0178", "alive", "410c0ae0")], "010d": [("alive", "410d07")]}, "--cycles", "3",
        first=4),
    "the gateway goes silent": stream(S, SHIFT_TABLE, "--cycles", "40", silent_from=10, first=2, slack=STREAM),
    "the gateway closes every ninth request": stream(S, {"010c": RPM, "010d": ["410d00"]}, "--cycles", "60",
                                                     close_every=9, first=2, slack=STREAM),
    "the lease held throughout": stream(S, SHIFT_TABLE, "--cycles", "30", lease={}, first=2),
    "the lease given up after 0.3 s": stream(S, {"010c": RPM, "010d": ["410d00"]}, "--cycles", "50",
                                             lease={"release_after": 0.3}, first=2, slack=STREAM),
    "another worker asks for the lease": stream(S, {"010c": RPM, "010d": ["410d00"]}, "--cycles", "80",
                                                waiter={"start_after": 0.6}, first=2, slack=STREAM),
    "SIGTERM part way": stream(S, SHIFT_TABLE, term_after=1.0, first=2, slack=STREAM),
    "SIGTERM while a read waits": stream(S, SHIFT_TABLE, term_after=0.8, silent_from=6, first=2, slack=STREAM),
    "its parent goes": orphaned(S),
    "the gateway moved and is saved": moved(S, "--cycles", "60"),
    "no gateway, nothing to search": stream(S, SHIFT_TABLE, "--cycles", "8", "--search-dev", "nonexistent0",
                                            host="127.0.0.2", first=2),
    "no arguments": arguments_only(),
    "host without a value": arguments_only("--host"),
    "two hosts": arguments_only("--host", "127.0.0.1", "--host", "127.0.0.2", "--cycles", "1"),
    "unknown option": arguments_only("--host", "127.0.0.1", "--verbose"),
    "cycles 1001": arguments_only("--host", "127.0.0.1", "--cycles", "1001"),
    "cycles 0": arguments_only("--host", "127.0.0.1", "--cycles", "0"),
    "port 0": arguments_only("--port", "0", "--host", "127.0.0.1"),
    "UDP port 65536": arguments_only("--host", "127.0.0.1", "--udp-port", "65536"),
    "search port with letters": arguments_only("--host", "127.0.0.1", "--search-port", "x"),
    "host too long": arguments_only("--host", "169.254.100.1000", "--cycles", "1"),
}


def needed(keys):
    """The measurements both runs must have. A gap needs two requests of a kind, which a slow reading near the end of a
    run may or may not get; the kinds of request are compared exactly."""
    return sorted(key for key in keys if not key.endswith("median gap, ms"))


class GaugeEquivalence:
    worker = None
    scenarios = None

    def compare(self, name):
        for reference_label, expected, label, result in pairs(self.worker, self.scenarios[name], name):
            expected, result = dict(expected), dict(result)
            for who, outcome in ((reference_label, expected), (label, result)):
                self.assertIs(outcome.get("only reads", True), True, f"{who} sent the gateway more than reads")
            reference_timing, timing = expected.pop("timing"), result.pop("timing")
            self.assertEqual(reference.plain(result), reference.plain(expected),
                             f"{label} differs from {reference_label}")
            self.assertEqual(needed(timing), needed(reference_timing), f"{label} measured other things")
            for key, measured in timing.items():
                if key not in reference_timing:
                    continue
                self.assertTrue(measured.agrees(reference_timing[key]),
                                f"{key}: {label} {measured}, {reference_label} {reference_timing[key]}")
            self.assertNotIn(expected.get("code"), CRASHED, f"{reference_label} crashed")


class BoostDeckEquivalence(GaugeEquivalence, unittest.TestCase):
    worker = B
    scenarios = BOOSTDECK


class ShiftDeckEquivalence(GaugeEquivalence, unittest.TestCase):
    worker = S
    scenarios = SHIFTDECK


add_tests(BoostDeckEquivalence, BOOSTDECK)
add_tests(ShiftDeckEquivalence, SHIFTDECK)
# For tests/record_fixtures.py: both gauge workers' scenarios.
SCENARIOS = {B: BOOSTDECK, S: SHIFTDECK}


def setUpModule():
    if len(builds()) < 2 and os.environ.get("ENA_REFERENCE") != "fixtures":
        raise unittest.SkipTest("ENA_WORKERS names one build: nothing to compare (tests/run_rust.py names two)")


if __name__ == "__main__":
    unittest.main()
