"""The bm3-maps worker against a simulated DME with bootmod3's map slot, like idrive7-apps' test_bm3_protocol.js
fixture. Checks that nothing is written unless the marker names a known ROM, and that a map change is read back."""
from pathlib import Path
import socket
import struct
import subprocess
import threading
import unittest

from test_transport import exact, frame

WORKER = Path(__file__).resolve().parents[1] / "build" / "bm3-maps"
MARKER = 0x807FFE50
SLOT = 0x4005E006  # R1C900000's map slot (bm3_profiles.h)


def legacy_marker(rom):
    return (b"BM3_R#" + rom.encode() + b"#").ljust(255, b"\0")


class FakeDme:
    """Answers 2C, 22, 23 and 3E/3D like a bootmod3 DME. refuse: SIDs answered 7F xx 31. silent: SIDs not answered."""

    def __init__(self, marker, slot=0, refuse=(), silent=(), stuck=False):
        self.memory = {MARKER: marker, SLOT: bytes([slot, 1])}
        self.refuse, self.silent, self.stuck = set(refuse), set(silent), stuck
        self.requests, self.entries = [], 0

    def answer(self, uds):
        sid = uds[0]
        self.requests.append(uds.hex())
        if sid in self.silent:
            return []
        if sid in self.refuse:
            return [bytes([0x7F, sid, 0x31])]
        if sid == 0x2C:
            self.entries = 0 if uds[1] == 3 else self.entries + 1
            return [bytes([0x6C]) + uds[1:4]]
        if sid == 0x22:
            return [bytes.fromhex("62f301") + self.memory[MARKER] * self.entries]
        if sid == 0x23:
            address, length = int.from_bytes(uds[2:6], "big"), uds[6]
            return [bytes([0x63]) + self.memory[address][:length]]
        if sid in (0x3D, 0x3E):
            address, length = int.from_bytes(uds[2:6], "big"), uds[6]
            if len(uds) != 7 + length:
                raise AssertionError(f"write length {length} with {len(uds) - 7} bytes")
            if not self.stuck:
                self.memory[address] = uds[7:]
            # "pending" first, as a DME busy writing would answer, then the positive reply
            return [bytes([0x7F, sid, 0x78]), bytes([0x7E, 0x3D]) if sid == 0x3E else bytes([0x7D])]
        raise AssertionError(f"unexpected service {sid:02x}")


class Bm3WorkerTest(unittest.TestCase):
    def run_worker(self, dme, *args):
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(3)

            def gateway():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(8)
                        while True:
                            header = conn.recv(6, socket.MSG_WAITALL)
                            if not header:
                                return
                            size, control = struct.unpack(">IH", header)
                            body = exact(conn, size)
                            self.assertEqual((control, body[:2]), (1, b"\xf4\x12"))
                            for reply in dme.answer(body[2:]):
                                conn.sendall(frame(1, b"\x12\xf4" + reply))
                except Exception as error:  # noqa: BLE001
                    errors.append(error)

            thread = threading.Thread(target=gateway)
            thread.start()
            result = subprocess.run([WORKER, "--host", "127.0.0.1", "--port", str(listener.getsockname()[1]),
                                     "--marker", f"{MARKER:08X}", *args], capture_output=True, text=True, timeout=30)
            thread.join(10)
            if errors:
                raise errors[0]
            return result.returncode, result.stdout.strip()

    def writes(self, dme):
        return [r for r in dme.requests if r[:2] in ("3d", "3e")]

    def test_status_reads_the_rom_and_the_slot(self):
        dme = FakeDme(legacy_marker("R1C900000"), slot=1)
        self.assertEqual(self.run_worker(dme, "--status"), (0, "Map 2 active (R1C900000)"))
        self.assertEqual(dme.requests, ["2c03f301", "2c02f30124807ffe5000ff", "22f301", "23144005e00601"])

    def test_map_change_writes_the_slot_and_reads_it_back(self):
        dme = FakeDme(legacy_marker("R1C900000"), slot=0)
        self.assertEqual(self.run_worker(dme, "--map", "3"), (0, "Map 3 active (R1C900000)"))
        self.assertEqual(self.writes(dme), ["3e144005e006020201"])
        self.assertEqual(dme.memory[SLOT], bytes([2, 1]))

    def test_unknown_rom_writes_nothing(self):
        dme = FakeDme(legacy_marker("ZZZZZZZZZ"))
        code, line = self.run_worker(dme, "--map", "2")
        self.assertEqual(code, 1)
        self.assertIn("No known bootmod3 ROM", line)
        self.assertEqual(self.writes(dme), [])

    def test_stock_dme_writes_nothing(self):
        dme = FakeDme(b"\0" * 255, refuse={0x2C, 0x23})
        code, line = self.run_worker(dme, "--map", "1")
        self.assertEqual((code, line), (1, "The DME refused to read its memory (NRC 31)"))
        self.assertEqual(self.writes(dme), [])

    def test_refused_dynamic_did_reads_the_marker_directly(self):
        dme = FakeDme(legacy_marker("R1C900000"), slot=3, refuse={0x2C})
        self.assertEqual(self.run_worker(dme, "--status"), (0, "Map 4 active (R1C900000)"))
        self.assertEqual(dme.requests[:2], ["2c03f301", "2314807ffe50ff"])

    def test_accumulated_marker_is_refused(self):
        dme = FakeDme(legacy_marker("R1C900000"))
        original = dme.answer

        def no_clear(uds):  # a DME that keeps earlier definitions: the DID returns two markers
            replies = original(uds)
            if uds[0] == 0x2C and uds[1] == 2:
                dme.entries = 2
            return replies
        dme.answer = no_clear
        code, line = self.run_worker(dme, "--map", "2")
        self.assertEqual((code, line), (1, "No known bootmod3 ROM: marker length"))
        self.assertEqual(self.writes(dme), [])

    def test_map_change_not_taken_is_reported(self):
        dme = FakeDme(legacy_marker("R1C900000"), slot=0, stuck=True)
        self.assertEqual(self.run_worker(dme, "--map", "2"),
                         (1, "Map change not confirmed: the DME reports map 1"))

    def test_refused_write(self):
        dme = FakeDme(legacy_marker("R1C900000"), refuse={0x3E})
        self.assertEqual(self.run_worker(dme, "--map", "2"), (1, "The DME refused the map change (NRC 31)"))


if __name__ == "__main__":
    unittest.main()
