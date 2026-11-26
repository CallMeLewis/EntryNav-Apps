r"""The C and Rust builds of ena-gateway and engine-codes, side by side: each scenario runs every build in ENA_WORKERS
against the same simulated gateway and compares them with the first, the C. They must print the same stdout and
stderr, exit with the same code, send the gateway the same requests, write the same files and take as long when they
wait out a timeout. Error paths included: no gateway, a gateway that refuses, closes or never answers, the lease held
by another worker, SIGTERM part way, and wrong arguments.

Needs two builds, as tests/run_rust.py runs it:
    ENA_WORKERS=c=build-arm,rust=build/rust ENA_WORKER_RUNNER="qemu-arm -L /sysroot ..." \
        python3 tests/test_worker_equivalence.py
"""

import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_transport import exact, frame
from workers import builds, command

LEASE_PORT = 42861
IDENT = b"DIAGADR10BMWMAC001122334455BMWVINWBA00000000000000"
READ = "1902af"
CLEAR = "14ffffff"


def dme(hex_body):
    """A diagnostic message from the DME (12) to the tester (F4)."""
    return frame(1, bytes.fromhex("12f4" + hex_body))


def identification(text=IDENT, control=0x11):
    return struct.pack(">IH", len(text), control) + text


class Gateway:
    """HSFZ on 127.0.0.1: answers each request from answers (UDS hex -> list of answers, used in turn, the last
    repeating). An answer is a list of raw frames to send, or "close" or "silent". Unknown requests get no answer. It
    records every frame it reads, as (control, body hex)."""

    def __init__(self, answers):
        self.answers = {request: list(replies) for request, replies in answers.items()}
        self.frames = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
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
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(12)
                try:
                    self.talk(conn)
                except (OSError, EOFError):
                    pass

    def talk(self, conn):
        while True:
            header = conn.recv(6, socket.MSG_WAITALL)
            if len(header) < 6:
                return
            size, control = struct.unpack(">IH", header)
            body = exact(conn, size)
            self.frames.append((control, body.hex()))
            if control != 1:
                continue
            replies = self.answers.get(body[2:].hex())
            if not replies:
                continue
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if reply == "close":
                return
            if reply == "silent":
                continue
            for raw in reply:
                # In two pieces, so that a worker reading a frame in one go is caught.
                conn.sendall(raw[:3])
                conn.sendall(raw[3:])

    def stop(self):
        self.done.set()
        self.thread.join(5)
        self.listener.close()


class Responder:
    """UDP on 0.0.0.0 that answers the first identification request with replies, from 127.0.0.1, and with extra
    gateways' identifications from 127.0.0.2 onwards."""

    def __init__(self, replies, extra=0):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", 0))
        self.socket.settimeout(6)
        self.port = self.socket.getsockname()[1]
        self.received = []
        self.replies, self.extra = replies, extra
        self.thread = threading.Thread(target=self.answer, daemon=True)
        self.thread.start()

    def answer(self):
        try:
            data, sender = self.socket.recvfrom(64)
        except OSError:
            return
        self.received.append(data.hex())
        for reply in self.replies:
            self.socket.sendto(reply, sender)
        for k in range(self.extra):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                other.bind((f"127.0.0.{k + 2}", 0))
                other.sendto(identification(), sender)

    def stop(self):
        self.thread.join(6)
        self.socket.close()


class Lease:
    """Another worker holding the diagnostic lease: bound to 127.0.0.1:42861, for release_after seconds or the whole
    run. It counts the "waiting" bytes it was sent."""

    def __init__(self, release_after=None):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", LEASE_PORT))
        self.socket.setblocking(False)
        self.asked = 0
        self.lock = threading.Lock()
        self.timer = threading.Timer(release_after, self.release) if release_after else None
        if self.timer:
            self.timer.start()

    def release(self):
        with self.lock:
            if self.socket.fileno() >= 0:
                self.count()
                self.socket.close()

    def count(self):
        while True:
            try:
                self.asked += len(self.socket.recv(16))
            except OSError:
                return

    def stop(self):
        if self.timer:
            self.timer.cancel()
        self.release()


def unused_port():
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        return unused.getsockname()[1]


def run(worker, args, term_after=None, timeout=30):
    """The worker's stdout, stderr (its path as WORKER), exit code and run time."""
    started = time.monotonic()
    process = subprocess.Popen([*worker, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if term_after is not None:
        time.sleep(term_after)
        process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=timeout)
    return {"stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace").replace(worker[-1], "WORKER"),
            "code": process.returncode, "seconds": time.monotonic() - started}


# ---- the scenarios: each takes a build's command line and returns what to compare ---------------------------------

def engine_codes(answers, *args, term_after=None, lease=None, host="127.0.0.1"):
    def scenario(worker):
        gateway = Gateway(answers)
        holder = Lease(**lease) if lease is not None else None
        try:
            result = run(worker, ["--host", host, "--port", str(gateway.port), *args], term_after)
        finally:
            gateway.stop()
            if holder:
                holder.stop()
        result["gateway"] = gateway.frames
        if holder:
            result["asked for the lease"] = holder.asked > 0
        return result
    return scenario


def engine_codes_moved(worker):
    """The saved address takes no connection: the worker asks lo, finds the gateway on 127.0.0.1 and saves it."""
    responder = Responder([identification()])
    with tempfile.TemporaryDirectory() as data:
        saved = Path(data) / "gateway-ip.txt"
        scenario = engine_codes({READ: [[dme("5902ff1a2f0f2f")]]}, "--codes", "--search-dev", "lo", "--search-port",
                                str(responder.port), "--gateway-file", str(saved), host="127.0.0.2")
        result = scenario(worker)
        result["saved"] = saved.read_text() if saved.exists() else None
        result["left behind"] = sorted(p.name for p in Path(data).iterdir())
    responder.stop()
    result["identification requests"] = responder.received
    return result


def arguments_only(*args):
    def scenario(worker):
        return run(worker, list(args))
    return scenario


def discover(replies, *args, extra=0, term_after=None):
    def scenario(worker):
        responder = Responder(replies, extra)
        try:
            result = run(worker, ["discover", *[str(responder.port) if a == "PORT" else a for a in args]], term_after)
        finally:
            responder.stop()
        result["identification requests"] = responder.received
        return result
    return scenario


def check(answers, term_after=None, lease=None, port=None):
    def scenario(worker):
        gateway = Gateway(answers)
        holder = Lease(**lease) if lease is not None else None
        try:
            result = run(worker, ["check", "--host", "127.0.0.1", "--port", str(port or gateway.port)], term_after)
        finally:
            gateway.stop()
            if holder:
                holder.stop()
        result["gateway"] = gateway.frames
        if holder:
            result["asked for the lease"] = holder.asked > 0
        return result
    return scenario


NOISE = [frame(2, bytes.fromhex("f412" + READ)), frame(1, bytes.fromhex("99f45902ff")), dme("7f1978")]
MANY = "5902ff" + "".join(f"{i:06X}08" for i in range(130))

ENGINE_CODES = {
    "codes with noise before them": engine_codes({READ: [NOISE + [dme("5902ff1a2f0f2f22222250" + "30fe0108")]]},
                                                 "--codes"),
    "no codes": engine_codes({READ: [[dme("5902ff")]]}, "--codes"),
    "130 codes, 128 shown": engine_codes({READ: [[dme(MANY)]]}, "--codes"),
    "availability mask": engine_codes({READ: [[dme("5902011a2f0f2f30fe0108")]]}, "--codes"),
    "an answer of the wrong length": engine_codes({READ: [[dme("5902ff1a2f0f")]]}, "--codes"),
    "read refused": engine_codes({READ: [[dme("7f1931")]]}, "--codes"),
    "alive check while waiting": engine_codes({READ: [[frame(0x12, b"\0\xf4"), dme("5902ff30fe0108")]]}, "--codes"),
    "oversized frame": engine_codes({READ: [[struct.pack(">IH", 500000, 1)]]}, "--codes"),
    "gateway closes": engine_codes({READ: ["close"]}, "--codes"),
    "gateway never answers (1.8 s)": engine_codes({}, "--codes"),
    "SIGTERM while waiting": engine_codes({}, "--codes", term_after=0.6),
    "clear, then read": engine_codes({CLEAR: [[dme("7f1478"), dme("54")]], READ: [[dme("5902ff")]]}, "--clear"),
    "clear in the extended session": engine_codes({CLEAR: [[dme("7f1422")], [dme("54")]],
                                                   "1003": [[dme("5003001e01f4")]], "1001": [[dme("5001001e01f4")]],
                                                   READ: [[dme("5902ff")]]}, "--clear"),
    "clear refused, session refused": engine_codes({CLEAR: [[dme("7f1422")]], "1003": [[dme("7f1012")]]}, "--clear"),
    "clear rejected": engine_codes({CLEAR: [[dme("7f1413")]]}, "--clear"),
    "clear answer of the wrong length": engine_codes({CLEAR: [[dme("5400")]]}, "--clear"),
    "clear, gateway closes": engine_codes({CLEAR: ["close"]}, "--clear"),
    "clear acknowledged, read fails": engine_codes({CLEAR: [[dme("54")]], READ: [[dme("7f1931")]]}, "--clear"),
    "clear acknowledged, no read answer": engine_codes({CLEAR: [[dme("54")]], READ: ["close"]}, "--clear"),
    "lease held throughout": engine_codes({READ: [[dme("5902ff")]]}, "--codes", lease={}),
    "lease given up after 0.2 s": engine_codes({READ: [[dme("5902ff")]]}, "--codes", lease={"release_after": 0.2}),
    "SIGTERM while waiting for the lease": engine_codes({}, "--codes", lease={}, term_after=0.3),
    "no gateway, search on lo finds nothing": lambda w: run(w, ["--host", "127.0.0.1", "--port", str(unused_port()),
                                                                "--codes", "--search-dev", "lo", "--search-port",
                                                                str(unused_port())]),
    "no gateway, no vbr0 to search": lambda w: run(w, ["--host", "127.0.0.1", "--port", str(unused_port()),
                                                       "--codes"]),
    "the gateway moved and is saved": engine_codes_moved,
    "no arguments": arguments_only(),
    "host only": arguments_only("--host", "127.0.0.1"),
    "mode only": arguments_only("--codes"),
    "two modes": arguments_only("--host", "127.0.0.1", "--codes", "--clear"),
    "two hosts": arguments_only("--host", "127.0.0.1", "--host", "127.0.0.2", "--codes"),
    "host without a value": arguments_only("--codes", "--host"),
    "host too long": arguments_only("--host", "169.254.100.1000", "--codes"),
    "empty host": arguments_only("--host", "", "--codes"),
    "port 0": arguments_only("--host", "127.0.0.1", "--port", "0", "--codes"),
    "port 65536": arguments_only("--host", "127.0.0.1", "--port", "65536", "--codes"),
    "port with letters": arguments_only("--host", "127.0.0.1", "--port", "68O1", "--codes"),
    "port with a space": arguments_only("--host", "127.0.0.1", "--port", "6801 ", "--codes"),
    "port that wraps on ARM": arguments_only("--host", "127.0.0.1", "--port", "-4294965495", "--codes",
                                             "--search-dev", "lo", "--search-port", "1"),
    "port far too large": arguments_only("--host", "127.0.0.1", "--port", "99999999999999999999", "--codes"),
    "search port 0": arguments_only("--host", "127.0.0.1", "--search-port", "0", "--codes"),
    "unknown option": arguments_only("--host", "127.0.0.1", "--codes", "--verbose"),
    "host with a leading zero": arguments_only("--host", "127.0.0.01", "--codes", "--search-dev", "lo",
                                               "--search-port", "1"),
    "host with three parts": arguments_only("--host", "127.0.1", "--codes", "--search-dev", "lo",
                                            "--search-port", "1"),
}

ENA_GATEWAY = {
    "discover: the gateway, once": discover([identification(), identification()], "--to", "127.0.0.1", "--port",
                                            "PORT", "--wait", "400"),
    "discover: other controllers and messages": discover([identification(b"DIAGADR62BMWMAC001122334455"),
                                                          identification(control=0x12), b"\x00\x11"],
                                                         "--to", "127.0.0.1", "--port", "PORT", "--wait", "400"),
    "discover: every network on lo": discover([identification()], "--dev", "lo", "--port", "PORT", "--wait", "400"),
    "discover: 18 gateways, 16 kept": discover([identification()], "--to", "127.0.0.1", "--port", "PORT", "--wait",
                                               "600", extra=17),
    "discover: nothing answers": discover([], "--to", "127.0.0.1", "--port", "PORT", "--wait", "300"),
    "discover: SIGTERM while waiting": discover([], "--to", "127.0.0.1", "--port", "PORT", "--wait", "5000",
                                                term_after=0.6),
    "discover: a device without an address": arguments_only("discover", "--dev", "nonexistent0", "--wait", "100"),
    "discover: an empty device": arguments_only("discover", "--dev", "", "--wait", "100"),
    "discover: a bad address": arguments_only("discover", "--to", "1.2.3", "--wait", "100"),
    "check: positive": check({"0100": [[dme("4100be3fa813")]]}),
    "check: pending, then refused": check({"0100": [[dme("7f0178"), dme("7f0112")]]}),
    "check: an answer too long to take": check({"0100": [[dme("4100" + "00" * 20)]]}),
    "check: never answers (1.5 s)": check({}),
    "check: gateway closes": check({"0100": ["close"]}),
    "check: no connection": check({}, port=unused_port()),
    "check: lease held": check({"0100": [[dme("4100be3fa813")]]}, lease={}),
    "check: SIGTERM while waiting": check({}, term_after=0.6),
    "no arguments": arguments_only(),
    "unknown command": arguments_only("scan"),
    "discover with --host": arguments_only("discover", "--host", "127.0.0.1"),
    "check with --to": arguments_only("check", "--to", "127.0.0.1"),
    "check without --host": arguments_only("check"),
    "check, --host without a value": arguments_only("check", "--host"),
    "check, two hosts": arguments_only("check", "--host", "127.0.0.1", "--host", "127.0.0.2"),
    "discover, two destinations": arguments_only("discover", "--to", "127.0.0.1", "--to", "127.0.0.2"),
    "wait too short": arguments_only("discover", "--wait", "99"),
    "wait too long": arguments_only("discover", "--wait", "10001"),
    "port 0": arguments_only("check", "--host", "127.0.0.1", "--port", "0"),
    "check, bad host": arguments_only("check", "--host", "127.0.0.256", "--port", "6801"),
}

# Scenarios that wait out a timeout: each build must take as long, within this many seconds (QEMU's start varies).
TIMED = {"gateway never answers (1.8 s)", "lease held throughout", "check: never answers (1.5 s)",
         "discover: nothing answers", "no gateway, search on lo finds nothing"}
TOLERANCE = 0.4


class Equivalence:
    worker = None
    scenarios = None

    def compare(self, name):
        results = {}
        for label, folder in builds():
            results[label] = self.scenarios[name](command(folder, self.worker))
        reference_label, reference = next(iter(results.items()))
        for label, result in results.items():
            seconds = result.pop("seconds")
            if label == reference_label:
                reference_seconds = seconds
                continue
            self.assertEqual(result, reference, f"{label} differs from {reference_label}")
            if name in TIMED:
                self.assertLess(abs(seconds - reference_seconds), TOLERANCE,
                                f"{label} took {seconds:.2f} s, {reference_label} {reference_seconds:.2f} s")
        self.assertNotIn(reference["code"], (-6, -11, 134, 139), "the reference crashed")


def add_tests(case, scenarios):
    for index, name in enumerate(scenarios):
        method = f"test_{index:02d}_" + "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
        setattr(case, method, lambda self, name=name: self.compare(name))


class EngineCodesEquivalence(Equivalence, unittest.TestCase):
    worker = "engine-codes"
    scenarios = ENGINE_CODES


class GatewayToolEquivalence(Equivalence, unittest.TestCase):
    worker = "ena-gateway"
    scenarios = ENA_GATEWAY


add_tests(EngineCodesEquivalence, ENGINE_CODES)
add_tests(GatewayToolEquivalence, ENA_GATEWAY)


def setUpModule():
    if len(builds()) < 2:
        raise unittest.SkipTest("ENA_WORKERS names one build: nothing to compare (tests/run_rust.py names two)")


if __name__ == "__main__":
    unittest.main()
