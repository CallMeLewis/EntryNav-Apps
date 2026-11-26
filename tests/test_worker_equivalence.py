r"""The C and Rust builds of the one-shot workers, ena-gateway, engine-codes, bm3-maps and mhd-maps, side by side: each
scenario runs every build in ENA_WORKERS against the same pretend gateway and DME and compares them with the first,
the C, or with what the C did as tests/fixtures records it (tests/reference.py). They must print the same stdout and
stderr, exit with the same code, send the gateway the same requests, byte for byte and in the same order, leave the
pretend DME's memory the same, write the same files and take as long when they wait out a timeout. Error paths
included: no gateway, a gateway that refuses, closes or never answers, the lease held by another worker, SIGTERM part
way, and wrong arguments. For bm3-maps, which writes to the DME, every write path: a map change accepted, refused with
any code, unanswered or not taken, burbles and Active Sound alike, and each reason nothing is written (an unknown ROM, a
stock or MHD-tuned DME, a lost DME, a profile whose addresses the DME refuses).

Needs two builds, or one and the fixtures, as tests/run_rust.py runs it:
    ENA_WORKERS=c=build-arm,rust=build/rust ENA_WORKER_RUNNER="qemu-arm -L /sysroot ..." \
        python3 tests/test_worker_equivalence.py
"""

import os
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

import reference
from test_bm3_worker import legacy_marker, mevd17_marker
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
    """HSFZ on 127.0.0.1. answers is either a table, UDS hex -> list of answers used in turn (the last repeating), an
    answer being a list of raw frames to send, or "close" or "silent"; or a function of the request's UDS bytes that
    returns such an answer. Unknown requests get no answer. It records every frame it reads, as (control, body hex)."""

    def __init__(self, answers):
        if callable(answers):
            self.answer = answers
        else:
            table = {request: list(replies) for request, replies in answers.items()}

            def lookup(uds):
                replies = table.get(uds.hex())
                if not replies:
                    return "silent"
                return replies.pop(0) if len(replies) > 1 else replies[0]
            self.answer = lookup
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
            reply = self.answer(body[2:])
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
        port = unused.getsockname()[1]
    reference.PORTS.add(port)  # a line that prints it compares as PORT
    return port


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


def moved(answers, *args):
    """The saved address takes no connection: the worker asks lo, finds the gateway on 127.0.0.1 and saves it."""
    def scenario(worker):
        responder = Responder([identification()])
        with tempfile.TemporaryDirectory() as data:
            saved = Path(data) / "gateway-ip.txt"
            fresh = answers() if callable(answers) and not isinstance(answers, dict) else answers
            result = engine_codes(fresh, *args, "--search-dev", "lo", "--search-port", str(responder.port),
                                  "--gateway-file", str(saved), host="127.0.0.2")(worker)
            result["saved"] = saved.read_text() if saved.exists() else None
            result["left behind"] = sorted(p.name for p in Path(data).iterdir())
        responder.stop()
        result["identification requests"] = responder.received
        return result
    return scenario


def arguments_only(*args):
    def scenario(worker):
        return run(worker, list(args))
    return scenario


def no_gateway(*args):
    """Nothing listens at the saved address, and the search on lo finds nothing (1.2 s)."""
    def scenario(worker):
        return run(worker, ["--host", "127.0.0.1", "--port", str(unused_port()), *args, "--search-dev", "lo",
                            "--search-port", str(unused_port())])
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
    "no gateway, search on lo finds nothing": no_gateway("--codes"),
    "no gateway, no vbr0 to search": lambda w: run(w, ["--host", "127.0.0.1", "--port", str(unused_port()),
                                                       "--codes"]),
    "the gateway moved and is saved": moved({READ: [[dme("5902ff1a2f0f2f")]]}, "--codes"),
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


# ---- bm3-maps: a pretend DME running a bootmod3 CustomROM ------------------------------------------------------------

MARKER = 0x807FFE50  # bootmod3's first address, MG1's
MEVD17_MARKER = 0x0977FE4C  # its second, where an F20 M140i keeps it
# The ROMs' addresses (src/bm3_profiles.h): map slot, burble switch, burble timing table.
R1C = (0x4005E006, 0x4005E099, 0x4005E100)  # R1C900000, written with 3E and answered 7E 3D
MG1 = (0x60038006, 0x60038099, 0x60038100)  # R4C9H5V02 and the S58's F4C9L8R02, 3E too
D001 = (0xD001D006, 0xD001D099, 0xD001D116)  # 011000, written with 3D and answered 7D
CAPTURED = bytes.fromhex("1141F2144D88227FC9D6EBF6C0CEE74104BFF6652CF4A3F310CDC33BB4EF1387D7DA9"
                         "11E2F2C0A906D91D631DE69E83FB14CF2A22CEA7161831C7D085133B3CC")


def binary_marker(token):
    """The MG1 layout (tests/test_bm3.c): BM3_, the captured 64-byte block, ~|~, 30 underscores, #token, NUL."""
    return (b"BM3_" + CAPTURED + b"~|~" + b"_" * 30 + b"#" + token.encode() + b"\0").ljust(255, b"\xff")


def hash_marker(tag):
    """The S58 layout (tests/test_bm3.c): BM3_, bytes without '#', the only '#' at byte 101, the tag, NUL."""
    block = bytes(0x24 if v == 0x23 else v for v in ((i * 37 + 11) & 0xFF for i in range(4, 101)))
    return (b"BM3_" + block + b"#" + tag.encode()).ljust(255, b"\0")


MHD_MARKER = (b"MHD+ MG1 multi-map " + bytes(range(64))).ljust(255, b"\xff")  # what an MHD-tuned DME has there


class Bm3Dme:
    """A DME with a bootmod3 CustomROM, behind the gateway, as tests/test_bm3_worker.py's FakeDme: its marker at `at`
    (2C 02 defines DID F301 there, 22 F3 01 reads it, 23 14 reads memory), and the ROM's map slot, burble switch and
    burble table at `rom`'s addresses. Writes (3D, 3E) go to memory and are answered "pending", then positively (7E 3D
    for 3E, 7D for 3D); a write or read at an address it doesn't have is refused (31, 72), and a 2C 02 for one too (33),
    as the car refused bootmod3's other addresses. 2E 5F FD is acknowledged.

    rules: request hex prefix -> answers, used in turn (the last repeating), in place of the usual answer; the first
    rule that matches counts. An answer is "close", "silent", or parts separated by spaces sent in order: UDS hex,
    "pending" (7F <SID> 78), "alive" (the gateway's alive check), "ack" (its acknowledgement) or "other" (another ECU's
    message). A write the rules answer still changes memory unless the answer refuses it or closes. stuck: writes are
    answered but change nothing."""

    def __init__(self, marker, at=MARKER, rom=R1C, slot=0, burbles=0, table=0, rules=None, stuck=False):
        slot_address, burble_address, timing_address = rom
        self.memory = {at: marker, slot_address: bytes([slot, 1]), burble_address: bytes([burbles]),
                       timing_address: bytes([table]) * 24}
        self.rules = [(prefix, list(answers)) for prefix, answers in (rules or {}).items()]
        self.stuck, self.defined, self.entries = stuck, None, 0

    def usual(self, uds):
        sid = uds[0]
        if sid == 0x2C:
            if uds[1] == 2:
                self.defined = int.from_bytes(uds[5:9], "big")
                if self.defined not in self.memory:
                    return f"7f{sid:02x}33"
            self.entries = 0 if uds[1] == 3 else self.entries + 1
            return "6c" + uds[1:4].hex()
        if sid == 0x22:
            return "62f301" + (self.memory.get(self.defined, b"") * self.entries).hex()
        if sid == 0x23:
            address, length = int.from_bytes(uds[2:6], "big"), uds[6]
            return "63" + self.memory[address][:length].hex() if address in self.memory else "7f2372"
        if sid in (0x3D, 0x3E):
            address = int.from_bytes(uds[2:6], "big")
            if address not in self.memory:
                return f"7f{sid:02x}31"
            return "pending " + ("7e3d" if sid == 0x3E else "7d")
        if sid == 0x2E:
            return "6e" + uds[1:3].hex()
        return f"7f{sid:02x}11"

    def __call__(self, uds):
        answer = None
        for prefix, answers in self.rules:
            if uds.hex().startswith(prefix):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                break
        if answer is None:
            answer = self.usual(uds)
        if uds[0] in (0x3D, 0x3E) and not self.stuck and answer != "close" and answer.split()[-1][:2] != "7f":
            address = int.from_bytes(uds[2:6], "big")
            if address in self.memory:
                self.memory[address] = uds[7:]
        if answer in ("close", "silent"):
            return answer
        raw = []
        for part in answer.split():
            if part == "pending":
                raw.append(dme(f"7f{uds[0]:02x}78"))
            elif part == "alive":
                raw.append(frame(0x12, b"\0\xf4"))
            elif part == "ack":
                raw.append(frame(2, b"\xf4\x12" + uds))
            elif part == "other":
                raw.append(frame(1, bytes.fromhex("40f4") + uds[:1] + b"\x00"))
            else:
                raw.append(dme(part))
        return raw

    def state(self):
        return {f"{address:08X}": data.hex() for address, data in sorted(self.memory.items())}


DATA_DIR = Path("/var/local/data/EntryNavApps")


@contextmanager
def marker_address_file(text):
    """The marker address set by hand, in the data folder the worker reads it from: only in a container (as root, with
    no data folder of its own), where it can't touch a real install."""
    if os.geteuid() != 0 or DATA_DIR.exists():
        raise unittest.SkipTest(f"{DATA_DIR} is only written in the test container")
    made = [folder for folder in (DATA_DIR, *DATA_DIR.parents) if not folder.exists()]
    DATA_DIR.mkdir(parents=True)
    try:
        (DATA_DIR / "bm3-marker-address").write_bytes(text)
        yield
    finally:
        (DATA_DIR / "bm3-marker-address").unlink(missing_ok=True)
        for folder in made:
            folder.rmdir()


def bm3(make, *args, term_after=None, lease=None, host="127.0.0.1", marker_file=None):
    """A scenario: bm3-maps with args against a fresh DME from make(). Compared: what engine_codes compares, and the
    DME's memory afterwards."""
    def scenario(worker):
        fake = make()
        if marker_file is None:
            result = engine_codes(fake, *args, term_after=term_after, lease=lease, host=host)(worker)
        else:
            with marker_address_file(marker_file):
                result = engine_codes(fake, *args, term_after=term_after, lease=lease, host=host)(worker)
        result["memory"] = fake.state()
        return result
    return scenario


def r1c(**options):
    """A DME running R1C900000 with the legacy marker at bootmod3's first address, map 1 active."""
    return lambda: Bm3Dme(legacy_marker("R1C900000"), **options)


def mevd17(**options):
    """The F20 M140i's DME: R1C900000 in the MEVD17 layout at bootmod3's second address."""
    return lambda: Bm3Dme(mevd17_marker("R1C900000CEKXA"), at=MEVD17_MARKER, **options)


def refused(code):
    return [f"pending 7f3e{code}"]


SLOT_READ = "23144005e006"  # R1C900000's map slot read
TABLE_WRITE, SWITCH_WRITE = "3e144005e100", "3e144005e099"
TABLE_READ, SWITCH_READ = "23144005e100", "23144005e099"

BM3_MAPS = {
    # Identifying the ROM: the requests --status sends, which write no memory.
    "status: the legacy marker, map 2 active": bm3(r1c(slot=1), "--status"),
    "status: MG1's binary marker": bm3(lambda: Bm3Dme(binary_marker("R4C9H5V02OKLX0"), rom=MG1, slot=3), "--status"),
    "status: the S58's hash marker": bm3(lambda: Bm3Dme(hash_marker("F4C9L8R02GGGJM_G87_RWD_USA_KOR_ULEV246_"),
                                                        rom=MG1, slot=2), "--status"),
    "status: the MEVD17 marker at the second address": bm3(mevd17(slot=2), "--status"),
    "status: the dynamic DID refused, the marker read directly": bm3(r1c(slot=3, rules={"2c": ["7f2c31"]}),
                                                                     "--status"),
    "status: the DID read refused, the marker read directly": bm3(r1c(rules={"22": ["7f2231"]}), "--status"),
    "status: no marker at any of the four addresses": bm3(lambda: Bm3Dme(legacy_marker("R1C900000"), at=0x12345678),
                                                          "--status"),
    "status: a stock DME refuses every read": bm3(r1c(rules={"2c": ["7f2c31"], "23": ["7f2331"]}), "--status"),
    "status: security access denied": bm3(r1c(rules={"2c": ["7f2c33"], "23": ["7f2333"]}), "--status"),
    "status: an unknown ROM": bm3(lambda: Bm3Dme(legacy_marker("ZZZZZZZZZ")), "--status"),
    "status: an MHD-tuned DME": bm3(lambda: Bm3Dme(MHD_MARKER), "--status"),
    "status: two ROMs in the marker": bm3(lambda: Bm3Dme(legacy_marker("R1C900000#F4C9G5700")), "--status"),
    "status: an S58 ROM with no profile": bm3(lambda: Bm3Dme(hash_marker("ZZZZZZZZZGGGJM_G87_RWD_USA_"), rom=MG1),
                                              "--status"),
    "status: a marker of the wrong length": bm3(r1c(rules={"22": ["62f301" + "00" * 254]}), "--status"),
    "status: a marker read beats a refusal": bm3(lambda: Bm3Dme(mevd17_marker("ZZZZZZZZZCEKXA"), at=MEVD17_MARKER),
                                                 "--status"),
    "status: a marker longer than the worker takes": bm3(r1c(rules={"22": ["62f301" + "41" * 1022]}), "--status"),
    "status: the DME stops answering (4.5 s)": bm3(r1c(rules={"2c02": ["silent"]}), "--status"),
    "status: the gateway closes during identification": bm3(r1c(rules={"22": ["close"]}), "--status"),
    "status: pending, alive checks, acknowledgements and other ECUs": bm3(
        r1c(slot=2, rules={"22": ["ack other pending alive 62f301" + legacy_marker("R1C900000").hex()]}),
        "--status"),
    "status: a refusal of another service is skipped": bm3(r1c(rules={"2c03": ["7f2231 6c03f301"]}), "--status"),
    "status: the slot read refused": bm3(r1c(rules={SLOT_READ: ["7f2331"]}), "--status"),
    "status: a slot out of range": bm3(r1c(rules={SLOT_READ: ["6304"]}), "--status"),
    "status: a slot answer of the wrong length": bm3(r1c(rules={SLOT_READ: ["630100"]}), "--status"),
    "status: a slot answer longer than the worker takes": bm3(r1c(rules={SLOT_READ: ["63" + "00" * 8]}), "--status"),
    "status, --dump": bm3(r1c(slot=1), "--status", "--dump"),
    "status, --dump: refusals and the search": bm3(mevd17(slot=2), "--dump", "--status"),
    "status, --dump: no answer": bm3(r1c(rules={"22": ["close"]}), "--status", "--dump"),
    "status, --marker: the MEVD17 address": bm3(mevd17(slot=1), "--status", "--marker", "0977FE4C"),
    "status, --marker in lower case": bm3(mevd17(slot=1), "--status", "--marker", "0977fe4c"),
    "status, --marker: nothing there": bm3(r1c(), "--status", "--marker", "801FFDAC"),
    "status, --marker with 0x": bm3(r1c(), "--status", "--marker", "0x7FFE50"),
    "status, --marker with a sign": bm3(r1c(), "--status", "--marker", "+807FFE5"),
    "status, --marker -0000001": bm3(r1c(), "--status", "--marker", "-0000001"),
    "status, an empty --marker: every address": bm3(mevd17(), "--status", "--marker", ""),
    "status: the marker address from the data folder": bm3(mevd17(slot=3), "--status",
                                                           marker_file=b"0977FE4C\r\nignored\n"),
    "status: an empty marker address file": bm3(r1c(), "--status", marker_file=b"\n"),
    "status: an invalid marker address file": bm3(r1c(), "--status", marker_file=b"0977FE4\n"),
    "status: a marker address file with a NUL": bm3(r1c(), "--status", marker_file=b"0977\0FE4C\n"),
    "status: a long line in the marker address file": bm3(r1c(), "--status", marker_file=b"9" * 40 + b"\n"),
    "status: the lease held": bm3(r1c(), "--status", lease={}),
    "status: no gateway": no_gateway("--status"),
    "status: the gateway moved and is saved": moved(r1c(slot=1), "--status"),
    "status: SIGTERM during identification": bm3(r1c(rules={"22": ["silent"]}), "--status", term_after=1.2),

    # --map: one write, then the slot read back
    "map 3 on a 3E ROM: written and read back": bm3(r1c(), "--map", "3"),
    "map 1 on a 3D ROM": bm3(lambda: Bm3Dme(legacy_marker("011000"), rom=D001, slot=2), "--map", "1"),
    "map 4 on MG1": bm3(lambda: Bm3Dme(binary_marker("R4C9H5V02OKLX0"), rom=MG1), "--map", "4"),
    "map 2 on the MEVD17": bm3(mevd17(), "--map", "2"),
    "map 2 with --marker": bm3(mevd17(), "--map", "2", "--marker", "0977FE4C"),
    "map 2: already active": bm3(r1c(slot=1), "--map", "2"),
    "map: refused, NRC 31": bm3(r1c(rules={"3e": refused("31")}), "--map", "2"),
    "map: refused, NRC 22": bm3(r1c(rules={"3e": refused("22")}), "--map", "2"),
    "map: refused, NRC 33": bm3(r1c(rules={"3e": refused("33")}), "--map", "2"),
    "map: refused, NRC 72": bm3(r1c(rules={"3e": refused("72")}), "--map", "2"),
    "map: refused, NRC 7F": bm3(r1c(rules={"3e": ["7f3e7f"]}), "--map", "2"),
    "map: refused on a 3D ROM": bm3(lambda: Bm3Dme(legacy_marker("011000"), rom=D001, rules={"3d": ["7f3d13"]}),
                                    "--map", "3"),
    "map: a profile whose slot the DME doesn't have": bm3(lambda: Bm3Dme(legacy_marker("R1C900000"), rom=MG1),
                                                          "--map", "2"),
    "map: the write unanswered (4.5 s), then read back": bm3(r1c(rules={"3e": ["silent"]}), "--map", "2"),
    "map: the write answered 7E 3E, skipped (4.5 s)": bm3(r1c(rules={"3e": ["7e3e"]}), "--map", "2"),
    "map: the write's answer too long": bm3(r1c(rules={"3e": ["7e3d" + "00" * 7]}), "--map", "2"),
    "map: the gateway closes on the write": bm3(r1c(rules={"3e": ["close"]}), "--map", "2"),
    "map: alive check and acknowledgement during the write": bm3(r1c(rules={"3e": ["ack alive pending 7e3d"]}),
                                                                 "--map", "2"),
    "map: not taken, the DME reports map 1": bm3(r1c(stuck=True), "--map", "2"),
    "map: the read back refused": bm3(r1c(rules={SLOT_READ: ["7f2331"]}), "--map", "2"),
    "map: the read back out of range": bm3(r1c(rules={SLOT_READ: ["6307"]}), "--map", "2"),
    "map: the read back unanswered (4.5 s)": bm3(r1c(rules={SLOT_READ: ["silent"]}), "--map", "2"),
    "map: an unknown ROM writes nothing": bm3(lambda: Bm3Dme(legacy_marker("ZZZZZZZZZ")), "--map", "2"),
    "map: a stock DME writes nothing": bm3(r1c(rules={"2c": ["7f2c31"], "23": ["7f2331"]}), "--map", "1"),
    "map: an MHD-tuned DME writes nothing": bm3(lambda: Bm3Dme(MHD_MARKER), "--map", "1"),
    "map: a lost DME writes nothing": bm3(r1c(rules={"22": ["close"]}), "--map", "4"),
    "map: no marker anywhere writes nothing": bm3(lambda: Bm3Dme(legacy_marker("R1C900000"), at=0x12345678),
                                                  "--map", "2"),
    "map: SIGTERM while the write waits": bm3(r1c(rules={"3e": ["silent"]}), "--map", "2", term_after=1.2),
    "map: SIGTERM while the read back waits": bm3(r1c(rules={SLOT_READ: ["silent"]}), "--map", "2", term_after=1.2),
    "map: the lease held": bm3(r1c(), "--map", "2", lease={}),
    "map: the lease given up after 0.2 s": bm3(r1c(), "--map", "2", lease={"release_after": 0.2}),
    "map: no gateway": no_gateway("--map", "2"),
    "map: the gateway moved and is saved": moved(r1c(), "--map", "3"),

    # --burbles: the table and the switch, in the order that keeps the DME off a half-written table, then read back
    "burbles 1, aggression 4": bm3(r1c(), "--burbles", "1"),
    "burbles 3 on a 3D ROM": bm3(lambda: Bm3Dme(legacy_marker("011000"), rom=D001), "--burbles", "3"),
    "burbles 2 on MG1": bm3(lambda: Bm3Dme(binary_marker("R4C9H5V02OKLX0"), rom=MG1), "--burbles", "2"),
    "burbles off": bm3(r1c(burbles=1, table=0xE8), "--burbles", "0"),
    "burbles: the first write refused": bm3(r1c(rules={"3e": refused("31")}), "--burbles", "2"),
    "burbles on: the switch refused": bm3(r1c(rules={SWITCH_WRITE: refused("22")}), "--burbles", "1"),
    "burbles off: the table refused": bm3(r1c(burbles=1, table=0xF0, rules={TABLE_WRITE: refused("72")}),
                                          "--burbles", "0"),
    "burbles: not taken": bm3(r1c(stuck=True), "--burbles", "3"),
    "burbles: the table reads back refused": bm3(r1c(rules={TABLE_READ: ["7f2331"]}), "--burbles", "1"),
    "burbles: the switch reads back wrong": bm3(r1c(rules={SWITCH_READ: ["6300"]}), "--burbles", "1"),
    "burbles: the table reads back short": bm3(r1c(rules={TABLE_READ: ["63" + "f8" * 23]}), "--burbles", "1"),
    "burbles: the first write unanswered (4.5 s)": bm3(r1c(rules={TABLE_WRITE: ["silent"]}), "--burbles", "2"),
    "burbles: the gateway closes on the switch": bm3(r1c(rules={SWITCH_WRITE: ["close"]}), "--burbles", "2"),
    "burbles: an unknown ROM writes nothing": bm3(lambda: Bm3Dme(legacy_marker("ZZZZZZZZZ")), "--burbles", "2"),
    "burbles: SIGTERM during the first write": bm3(r1c(rules={TABLE_WRITE: ["silent"]}), "--burbles", "2",
                                                   term_after=1.2),
    "burbles: the lease held": bm3(r1c(), "--burbles", "2", lease={}),

    # --sound: one request, 8 s to answer
    "sound on": bm3(r1c(), "--sound", "1"),
    "sound off": bm3(r1c(), "--sound", "0"),
    "sound on, after pending answers": bm3(r1c(rules={"2e": ["pending pending 6e5ffd"]}), "--sound", "1"),
    "sound: refused, NRC 31": bm3(r1c(rules={"2e": ["7f2e31"]}), "--sound", "1"),
    "sound: refused, NRC 22": bm3(r1c(rules={"2e": ["7f2e22"]}), "--sound", "0"),
    "sound: no answer (8 s)": bm3(r1c(rules={"2e": ["silent"]}), "--sound", "1"),
    "sound: the answer too long": bm3(r1c(rules={"2e": ["6e5ffd" + "00" * 14]}), "--sound", "1"),
    "sound: an unknown ROM sends nothing": bm3(lambda: Bm3Dme(legacy_marker("ZZZZZZZZZ")), "--sound", "1"),
    "sound: SIGTERM while waiting": bm3(r1c(rules={"2e": ["silent"]}), "--sound", "1", term_after=1.2),

    # Arguments: none of these reach the gateway
    "no arguments": arguments_only(),
    "host only": arguments_only("--host", "127.0.0.1"),
    "action only": arguments_only("--status"),
    "two actions": arguments_only("--host", "127.0.0.1", "--burbles", "1", "--sound", "1"),
    "status twice": arguments_only("--host", "127.0.0.1", "--status", "--status"),
    "map after status": arguments_only("--host", "127.0.0.1", "--status", "--map", "2"),
    "status after map": arguments_only("--host", "127.0.0.1", "--map", "2", "--status"),
    "map 0": arguments_only("--host", "127.0.0.1", "--map", "0"),
    "map 5": arguments_only("--host", "127.0.0.1", "--map", "5"),
    "map with letters": arguments_only("--host", "127.0.0.1", "--map", "two"),
    "map without a value": arguments_only("--host", "127.0.0.1", "--map"),
    "sound 2": arguments_only("--host", "127.0.0.1", "--sound", "2"),
    "burbles 4": arguments_only("--host", "127.0.0.1", "--burbles", "4"),
    "burbles -1": arguments_only("--host", "127.0.0.1", "--burbles", "-1"),
    "the flap isn't a control": arguments_only("--host", "127.0.0.1", "--flap", "1"),
    "two hosts": arguments_only("--host", "127.0.0.1", "--host", "127.0.0.2", "--status"),
    "host without a value": arguments_only("--status", "--host"),
    "host too long": arguments_only("--host", "169.254.100.1000", "--status"),
    "empty host": arguments_only("--host", "", "--map", "1"),
    "port 0": arguments_only("--host", "127.0.0.1", "--port", "0", "--status"),
    "port with letters": arguments_only("--host", "127.0.0.1", "--port", "68O1", "--status"),
    "port that wraps on ARM": arguments_only("--host", "127.0.0.1", "--port", "-4294965495", "--status",
                                             "--search-dev", "lo", "--search-port", "1"),
    "marker of 7 digits": arguments_only("--host", "127.0.0.1", "--marker", "807FFE5", "--map", "1"),
    "marker of 9 digits": arguments_only("--host", "127.0.0.1", "--marker", "807FFE500", "--map", "1"),
    "marker that isn't hex": arguments_only("--host", "127.0.0.1", "--marker", "807FFEZ0", "--map", "1"),
    "marker with a space": arguments_only("--host", "127.0.0.1", "--marker", " 807FFE5", "--status"),
    "marker far too long": arguments_only("--host", "127.0.0.1", "--marker", "8" * 40, "--status"),
    "marker without a value": arguments_only("--host", "127.0.0.1", "--status", "--marker"),
    "a bad marker without an action": arguments_only("--host", "127.0.0.1", "--marker", "x"),
    "search port 0": arguments_only("--host", "127.0.0.1", "--search-port", "0", "--status"),
    "dump only": arguments_only("--dump"),
    "a stray argument": arguments_only("--host", "127.0.0.1", "--status", "extra"),
}

# ---- mhd-maps: MHD's 2F requests, answered by their echo ---------------------------------------------------------------

RPM = "010c"


def mhd(answers, *args, term_after=None, lease=None):
    return engine_codes(answers, *args, term_after=term_after, lease=lease)


def rpm(value):
    return [[dme("410c" + (value * 4).to_bytes(2, "big").hex())]]


def echo(request):
    return {request: [[dme("6f" + request[2:])]]}


MHD_MAPS = {
    "map 1": mhd(echo("2f0100"), "--map", "1"),
    "map 2": mhd(echo("2f0101"), "--map", "2"),
    "map 3": mhd(echo("2f0102"), "--map", "3"),
    "map 4": mhd(echo("2f0103"), "--map", "4"),
    "map 2 after a pending answer": mhd({"2f0101": [[dme("7f2f78"), dme("6f0101")]]}, "--map", "2"),
    "map 2 after an acknowledgement, an alive check and another ECU": mhd(
        {"2f0101": [[frame(2, bytes.fromhex("f4122f0101")), frame(0x12, b"\0\xf4"),
                     frame(1, bytes.fromhex("40f46f0101")), dme("6f0101")]]}, "--map", "2"),
    "map 2: another map echoed (3 s)": mhd({"2f0101": [[dme("6f0100")]]}, "--map", "2"),
    "map 1: refused, NRC 11": mhd({"2f0100": [[dme("7f2f11")]]}, "--map", "1"),
    "map 1: refused, NRC 12": mhd({"2f0100": [[dme("7f2f12")]]}, "--map", "1"),
    "map 1: refused, NRC 13": mhd({"2f0100": [[dme("7f2f13")]]}, "--map", "1"),
    "map 1: refused, NRC 31": mhd({"2f0100": [[dme("7f2f31")]]}, "--map", "1"),
    "map 1: refused, NRC 22": mhd({"2f0100": [[dme("7f2f22")]]}, "--map", "1"),
    "map 1: refused, NRC 33": mhd({"2f0100": [[dme("7f2f33")]]}, "--map", "1"),
    "map 3: refused after a pending answer": mhd({"2f0102": [[dme("7f2f78"), dme("7f2f31")]]}, "--map", "3"),
    "map 4: no answer (3 s)": mhd({}, "--map", "4"),
    "map 4: the gateway closes": mhd({"2f0103": ["close"]}, "--map", "4"),
    "map 4: the echo too long": mhd({"2f0103": [[dme("6f0103" + "00" * 14)]]}, "--map", "4"),
    "map 4: an oversized frame": mhd({"2f0103": [[struct.pack(">IH", 500000, 1)]]}, "--map", "4"),
    "Max Cooling on at 750 rpm": mhd({RPM: rpm(750), **echo("2f0301")}, "--cooling", "1"),
    "Max Cooling on at 501 rpm": mhd({RPM: rpm(501), **echo("2f0301")}, "--cooling", "1"),
    "Max Cooling on at 500 rpm": mhd({RPM: rpm(500), **echo("2f0301")}, "--cooling", "1"),
    "Max Cooling on with the engine off": mhd({RPM: rpm(0), **echo("2f0301")}, "--cooling", "1"),
    "Max Cooling on: no engine speed (1 s)": mhd(echo("2f0301"), "--cooling", "1"),
    "Max Cooling on: engine speed refused": mhd({RPM: [[dme("7f0112")]], **echo("2f0301")}, "--cooling", "1"),
    "Max Cooling on: engine speed of the wrong length": mhd({RPM: [[dme("410c0b")]], **echo("2f0301")},
                                                            "--cooling", "1"),
    "Max Cooling on: engine speed too long": mhd({RPM: [[dme("410c0bb80000000000")]], **echo("2f0301")},
                                                 "--cooling", "1"),
    "Max Cooling on: refused": mhd({RPM: rpm(900), "2f0301": [[dme("7f2f31")]]}, "--cooling", "1"),
    "Max Cooling on: no echo (3 s)": mhd({RPM: rpm(900)}, "--cooling", "1"),
    "Max Cooling off": mhd(echo("2f0300"), "--cooling", "0"),
    "Max Cooling off: refused": mhd({"2f0300": [[dme("7f2f22")]]}, "--cooling", "0"),
    "the lease held throughout (650 ms)": mhd(echo("2f0101"), "--map", "2", lease={}),
    "the lease given up after 0.2 s": mhd(echo("2f0101"), "--map", "2", lease={"release_after": 0.2}),
    "SIGTERM while waiting for the lease": mhd(echo("2f0101"), "--map", "2", lease={}, term_after=0.3),
    "SIGTERM while waiting for the echo": mhd({}, "--map", "2", term_after=0.8),
    "SIGTERM while reading the engine speed": mhd({}, "--cooling", "1", term_after=0.5),
    "no gateway": no_gateway("--map", "1"),
    "the gateway moved and is saved": moved(echo("2f0102"), "--map", "3"),
    "no arguments": arguments_only(),
    "host only": arguments_only("--host", "127.0.0.1"),
    "action only": arguments_only("--map", "1"),
    "map 0": arguments_only("--host", "127.0.0.1", "--map", "0"),
    "map 5": arguments_only("--host", "127.0.0.1", "--map", "5"),
    "map without a value": arguments_only("--host", "127.0.0.1", "--map"),
    "cooling 2": arguments_only("--host", "127.0.0.1", "--cooling", "2"),
    "map, then cooling": arguments_only("--host", "127.0.0.1", "--map", "1", "--cooling", "0"),
    "cooling, then map": arguments_only("--host", "127.0.0.1", "--cooling", "0", "--map", "1"),
    "a status it doesn't have": arguments_only("--host", "127.0.0.1", "--status"),
    "two hosts": arguments_only("--host", "127.0.0.1", "--host", "127.0.0.2", "--map", "1"),
    "host too long": arguments_only("--host", "169.254.100.1000", "--map", "1"),
    "port 65536": arguments_only("--host", "127.0.0.1", "--port", "65536", "--map", "1"),
    "search port 0": arguments_only("--host", "127.0.0.1", "--search-port", "0", "--map", "1"),
    "a stray argument": arguments_only("--host", "127.0.0.1", "--map", "1", "extra"),
}

# Scenarios that wait out a timeout: each build must take as long, within this many seconds (QEMU's start varies).
TIMED = {"gateway never answers (1.8 s)", "lease held throughout", "check: never answers (1.5 s)",
         "discover: nothing answers", "no gateway, search on lo finds nothing", "status: no gateway", "map: no gateway",
         "no gateway"}
TIMED.update(name for name in (*BM3_MAPS, *MHD_MAPS) if " s)" in name or "(650 ms)" in name)
TOLERANCE = 0.4
CRASHED = (-6, -11, 134, 139)


def pairs(worker, scenario, name):
    """[(reference label, reference, label, result)]: each build's result against the reference's (tests/reference.py).
    Live, each build after the first against the first, then the first against its recording if it has one."""
    recorded = reference.recorded(worker, name)
    if reference.mode(worker) == "fixtures":
        if recorded is None:
            raise unittest.SkipTest(f"tests/fixtures has no {worker} result for this scenario")
        return [("the recorded C", recorded, label, scenario(command(folder, worker)))
                for label, folder in reference.present(worker)]
    runs = [(label, scenario(command(folder, worker))) for label, folder in builds()]
    (first, result), others = runs[0], runs[1:]
    compared = [(first, result, label, other) for label, other in others]
    if recorded is not None:
        compared.append(("its recording", recorded, first, result))
    return compared


class Equivalence:
    worker = None
    scenarios = None

    def compare(self, name):
        for reference_label, expected, label, result in pairs(self.worker, self.scenarios[name], name):
            expected, result = dict(expected), dict(result)
            expected_seconds, seconds = expected.pop("seconds"), result.pop("seconds")
            self.assertEqual(reference.plain(result), reference.plain(expected),
                             f"{label} differs from {reference_label}")
            if name in TIMED:
                self.assertLess(abs(seconds - expected_seconds), TOLERANCE,
                                f"{label} took {seconds:.2f} s, {reference_label} {expected_seconds:.2f} s")
            self.assertNotIn(expected["code"], CRASHED, f"{reference_label} crashed")


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


class Bm3MapsEquivalence(Equivalence, unittest.TestCase):
    worker = "bm3-maps"
    scenarios = BM3_MAPS


class MhdMapsEquivalence(Equivalence, unittest.TestCase):
    worker = "mhd-maps"
    scenarios = MHD_MAPS


add_tests(EngineCodesEquivalence, ENGINE_CODES)
add_tests(GatewayToolEquivalence, ENA_GATEWAY)
add_tests(Bm3MapsEquivalence, BM3_MAPS)
add_tests(MhdMapsEquivalence, MHD_MAPS)
# For tests/record_fixtures.py: every one-shot worker's scenarios.
SCENARIOS = {"engine-codes": ENGINE_CODES, "ena-gateway": ENA_GATEWAY, "bm3-maps": BM3_MAPS, "mhd-maps": MHD_MAPS}


def setUpModule():
    if len(builds()) < 2 and os.environ.get("ENA_REFERENCE") != "fixtures":
        raise unittest.SkipTest("ENA_WORKERS names one build: nothing to compare (tests/run_rust.py names two)")


if __name__ == "__main__":
    unittest.main()
