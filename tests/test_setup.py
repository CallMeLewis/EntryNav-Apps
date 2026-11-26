"""Each plugin's setup.sh on a pretend EntryNav2 (tests/support/headunit.py), with the archives from
tools/package_release.py.

Run through tests/run_setup.py, which starts the privileged QEMU container this needs.
"""

import hashlib
import os
import socket
import struct
import sys
import tarfile
import threading
import unittest
from pathlib import Path

from support.headunit import Unit

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
# The archives to test: dist, as tools/package_release.py writes them, or another folder (ENA_DIST, relative to the
# repository), such as dist/rust for the archives with the Rust workers.
DIST = ROOT / os.environ.get("ENA_DIST", "dist")
PLUGINS = {"boostdeck": "BoostDeck", "shiftdeck": "ShiftDeck", "bm3-maps": "Bm3Maps", "mhd-maps": "MhdMaps",
           "engine-codes": "EngineCodes"}
WORKERS = {"boostdeck": "boostdeck-collector", "shiftdeck": "shift-deck", "bm3-maps": "bm3-maps",
           "mhd-maps": "mhd-maps", "engine-codes": "engine-codes"}
MARK = ".installed-by-entrynav-apps"
RULE_HSFZ = "OUTPUT -o vbr0 -p tcp --dport 6801 -j ACCEPT"
RULE_HSFZ_OLD = "OUTPUT -d 169.254.0.0/16 -p tcp --dport 6801 -j ACCEPT"  # 0.1.0-test.1
RULES = [RULE_HSFZ, "OUTPUT -o vbr0 -p udp --dport 6811 -j ACCEPT", "INPUT -i vbr0 -p udp --sport 6811 -j ACCEPT"]
GATEWAY = "169.254.10.3"
DATA = "var/local/data/EntryNavApps"


def archive(plugin):
    return DIST / f"{plugin}-{VERSION}-armhf.tar"


def arm_program(*instructions):
    """A static 32-bit ARM EABI 5 hard-float program made of these instructions alone: an ELF header and one segment,
    as setup.sh check sees a Rust worker."""
    code = b"".join(struct.pack("<I", instruction) for instruction in instructions)
    size = 52 + 32 + len(code)
    header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01".ljust(16, b"\0"), 2, 40, 1, 0x10000 + 84, 52, 0,
                         0x05000400, 52, 32, 1, 0, 0, 0)
    return header + struct.pack("<8I", 1, 0, 0x10000, 0x10000, size, size, 5, 0x1000) + code


def exits(code):
    return (0xE3A00000 | code, 0xE3A07001, 0xEF000000)  # mov r0, #code; mov r7, #1 (exit); svc #0


UNDEFINED = (0xE7F000F0,)  # udf: SIGILL
FOREVER = (0xEAFFFFFE,)  # b .


def is_static(data):
    """Whether an ELF has neither an interpreter nor a dynamic section."""
    phoff, = struct.unpack_from("<I", data, 28)
    size, count = struct.unpack_from("<HH", data, 42)
    return all(struct.unpack_from("<I", data, phoff + i * size)[0] not in (2, 3) for i in range(count))


def setup(plugin):
    """The setup.sh of the unpacked archive."""
    return f"sh /tmp/{plugin}-{VERSION}/setup.sh"


def installed(plugin):
    return f"sh /opt/hmi/data/ro/Plugins/{PLUGINS[plugin]}/setup.sh"


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.unit = Unit()
        for plugin in PLUGINS:
            with tarfile.open(archive(plugin)) as tar:
                tar.extractall(self.unit.path("tmp"))

    def tearDown(self):
        self.unit.close()

    def sh(self, script, code=0):
        result = self.unit.sh(script)
        out = result.stdout + result.stderr
        if code is not None:
            self.assertEqual(result.returncode, code, out)
        self.assertTrue(self.unit.root_read_only(), "the system partition was left writable\n" + out)
        return out

    def install(self, plugin):
        out = self.sh(f"{setup(plugin)} install")
        self.assertIn(f"is installed", out)
        return out

    def app_dir(self, plugin):
        return self.unit.path(f"opt/hmi/data/ro/Plugins/{PLUGINS[plugin]}")

    def rules_file(self, plugin):
        return self.unit.path(f"etc/firewall/entrynav-{plugin}.rules")

    def assert_installed(self, plugin):
        folder = self.app_dir(plugin)
        self.assertTrue((folder / MARK).is_file(), plugin)
        self.assertTrue((folder / f"lib{PLUGINS[plugin]}.so").is_file(), plugin)
        self.assertTrue((folder / "setup.sh").is_file(), plugin)
        self.assertTrue(self.rules_file(plugin).read_text().startswith("# EntryNav-Apps "), plugin)

    def assert_absent(self, plugin):
        self.assertFalse(self.app_dir(plugin).exists(), plugin)
        self.assertFalse(self.rules_file(plugin).exists(), plugin)

    def leftovers(self):
        return sorted(p.name for p in self.unit.path("opt").iterdir() if p.name.startswith(".entrynav-"))

    # ---- check ---------------------------------------------------------------------------------------------------
    def release_file(self, plugin, name):
        return self.unit.path(f"tmp/{plugin}-{VERSION}/opt/hmi/data/ro/Plugins/{PLUGINS[plugin]}/{name}")

    def replace_in_release(self, plugin, name, data):
        """data in place of the app folder's file name in plugin's unpacked release, with MD5SUMS to match, as a
        release built that way would have them."""
        relative = f"opt/hmi/data/ro/Plugins/{PLUGINS[plugin]}/{name}"
        path = self.unit.path(f"tmp/{plugin}-{VERSION}/{relative}")
        path.write_bytes(data)
        path.chmod(0o755)
        sums = self.unit.path(f"tmp/{plugin}-{VERSION}/MD5SUMS")
        sums.write_text("".join(f"{hashlib.md5(data).hexdigest()}  {relative}\n" if line.endswith(f"  {relative}")
                                else line + "\n" for line in sums.read_text().splitlines()))

    def test_each_check_passes_and_writes_nothing(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin):
                out = self.sh(f"{setup(plugin)} check")
                self.assertIn("Every check passed", out)
                self.assertIn("every library and function the app uses exists on this car", out)
                for name in (WORKERS[plugin], "ena-gateway"):
                    static = is_static(self.release_file(plugin, name).read_bytes())
                    line = f"info  {name} is statically linked: it needs no library on this car, and it runs here"
                    (self.assertIn if static else self.assertNotIn)(line, out)
        self.assertEqual(list(self.unit.path("opt/hmi/data/ro/Plugins").iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.unit.path("etc/firewall").iterdir()),
                         ["HDRadio.rules", "sshd.rules", "whitelist.rules"])
        self.assertEqual(self.unit.rules(), [])

    def test_a_static_worker_runs_instead_of_being_traced(self):
        """glibc 2.15's loader crashes tracing a static program and then names nothing missing, so check runs a Rust
        worker instead: with no arguments it prints its usage and exits 2."""
        rust = ROOT / "build" / "rust"
        if not (rust / "bm3-maps").is_file():
            self.skipTest("build/rust has no Rust workers: make rust")
        for name in ("bm3-maps", "ena-gateway"):
            self.replace_in_release("bm3-maps", name, (rust / name).read_bytes())
        out = self.sh(f"{setup('bm3-maps')} check")
        self.assertIn("info  bm3-maps is statically linked: it needs no library on this car, and it runs here", out)
        self.assertIn("info  ena-gateway is statically linked", out)
        self.assertIn("ok    every library and function the app uses exists on this car", out)
        self.install("bm3-maps")
        self.assertIn("files: intact", self.sh(f"{installed('bm3-maps')} status"))

    def test_a_static_program_that_does_not_run_stops_the_install(self):
        """Each of these passed the loader's trace before, which crashed on it and printed nothing missing."""
        for program, why in ((exits(0), "(exit 0)"), (exits(1), "(exit 1)"), (UNDEFINED, "(exit 132)"),
                             (FOREVER, "(still running after 5 seconds)")):
            with self.subTest(why=why):
                self.replace_in_release("mhd-maps", "mhd-maps", arm_program(*program))
                out = self.sh(f"{setup('mhd-maps')} install", 1)
                self.assertIn(f"STOP  mhd-maps is statically linked but doesn't run on this car {why}", out)
                self.assertIn("Not installed: 1 check(s) failed.", out)
                self.assert_absent("mhd-maps")
        self.replace_in_release("mhd-maps", "mhd-maps", arm_program(*exits(2)))
        self.assertIn("info  mhd-maps is statically linked", self.sh(f"{setup('mhd-maps')} check"))

    def test_a_program_for_another_computer_stops_the_install(self):
        static_soft_float = bytearray(arm_program(*exits(2)))
        static_soft_float[36:40] = struct.pack("<I", 0x04000000)  # EABI 4: not the car's
        for data in (Path("/bin/true").read_bytes(), bytes(static_soft_float), b"#!/bin/sh\nexit 2\n"):
            with self.subTest(start=data[:8]):
                self.replace_in_release("engine-codes", "engine-codes", data)
                out = self.sh(f"{setup('engine-codes')} install", 1)
                self.assertIn("STOP  engine-codes is not a program for this head unit's 32-bit ARM Linux", out)
                self.assert_absent("engine-codes")

    def test_check_stops_on_a_writable_system(self):
        self.unit.writable()
        result = self.unit.sh(f"{setup('boostdeck')} check")
        self.unit.read_only()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the system partition is writable right now", result.stdout)

    def another_framework(self):
        """Another build of iDrive's app framework: the car's own library with a byte more, which still loads."""
        path = self.unit.path("opt/hmi/lib/libAppFramework.A.so")
        self.unit.put("opt/hmi/lib/libAppFramework.A.so", path.read_bytes() + b"\0")

    def test_another_app_framework_warns_and_installs(self):
        self.another_framework()
        out = self.sh(f"{setup('shiftdeck')} check")
        self.assertIn("warn  iDrive's app framework differs", out)
        self.assertIn("Every check passed", out)
        self.install("shiftdeck")
        self.assert_installed("shiftdeck")
        self.assertIn("app framework: as checked", self.sh(f"{installed('shiftdeck')} status"))

    def test_status_reports_a_framework_changed_since_the_install(self):
        self.install("shiftdeck")
        self.another_framework()
        self.assertIn("app framework: changed since the installation", self.sh(f"{installed('shiftdeck')} status"))

    def test_install_stops_on_a_damaged_release(self):
        path = self.unit.path(f"tmp/bm3-maps-{VERSION}/opt/hmi/data/ro/Plugins/Bm3Maps/bm3-maps")
        path.write_bytes(path.read_bytes() + b"x")
        self.assertIn("the release is damaged", self.sh(f"{setup('bm3-maps')} install", 1))
        self.assert_absent("bm3-maps")

    def test_folders_not_ours_are_left_alone(self):
        self.unit.put("opt/hmi/data/ro/Plugins/BoostDeck/libBoostDeck.so", b"someone else's")
        self.assertIn("was not installed by EntryNav-Apps: it is left alone", self.sh(f"{setup('boostdeck')} install", 1))
        self.assertEqual(self.unit.path("opt/hmi/data/ro/Plugins/BoostDeck/libBoostDeck.so").read_bytes(),
                         b"someone else's")
        self.assertFalse(self.rules_file("boostdeck").exists())

    def test_firewall_rules_not_ours_are_left_alone(self):
        self.unit.put("etc/firewall/entrynav-shiftdeck.rules", "# something else\n")
        out = self.sh(f"{setup('shiftdeck')} check", 1)
        self.assertIn("/etc/firewall/entrynav-shiftdeck.rules exists and was not installed", out)

    def test_update_takes_over_the_rule_from_before_the_rename(self):
        """BM3 Maps became Bootmod3 Suite: the rule file 0.1.0 installed still names BM3 Maps on its first line."""
        self.install("bm3-maps")
        rules = self.unit.path("etc/firewall/entrynav-bm3-maps.rules")
        old_mark = "# EntryNav-Apps BM3 Maps: lets it reach the car's diagnostic gateway."
        self.unit.put("etc/firewall/entrynav-bm3-maps.rules", old_mark + "\n" + rules.read_text().split("\n", 1)[1])
        self.sh(f"{setup('bm3-maps')} check")
        self.install("bm3-maps")
        self.assertEqual(rules.read_text().split("\n")[0],
                         "# EntryNav-Apps Bootmod3 Suite: lets it reach the car's diagnostic gateway.")

    # ---- one plugin at a time ------------------------------------------------------------------------------------
    def test_install_one_and_its_status(self):
        out = self.install("shiftdeck")
        self.assert_installed("shiftdeck")
        self.assert_absent("boostdeck")
        self.assert_absent("bm3-maps")
        self.assertEqual(self.leftovers(), [])
        self.assertIn("No gateway answered", out)  # nothing answers in the container
        self.assertEqual(self.unit.rules(), RULES)  # the search's rules stay, for the apps' own searches
        status = self.sh(f"{installed('shiftdeck')} status")
        for line in (f"installed: {VERSION}", "files: intact", "firewall rule: installed", "app framework: as checked",
                     "gateway: none"):
            self.assertIn(line, status)
        self.assertIn("installed: no", self.sh(f"{setup('boostdeck')} status"))

    def test_status_reports_damaged_files(self):
        self.install("shiftdeck")
        self.unit.put("opt/hmi/data/ro/Plugins/ShiftDeck/ShiftDeck.xml", "<broken/>")
        self.assertIn("files: damaged", self.sh(f"{installed('shiftdeck')} status"))

    def test_removing_one_keeps_the_others_and_the_shared_gateway(self):
        for plugin in PLUGINS:
            self.install(plugin)
            self.assert_installed(plugin)
        self.unit.path(f"{DATA}/gateway-ip.txt").write_text(GATEWAY + "\n")
        self.unit.path(f"{DATA}/bm3-marker-address").write_text("807FFE50\n")
        self.sh(f"{installed('bm3-maps')} uninstall --purge")
        self.assert_absent("bm3-maps")
        self.assert_installed("boostdeck")
        self.assert_installed("shiftdeck")
        self.assertFalse(self.unit.path(f"{DATA}/bm3-marker-address").exists())  # its own setting goes
        self.assertTrue(self.unit.path(f"{DATA}/gateway-ip.txt").exists())  # the shared one stays
        self.assertEqual(self.unit.rules(), RULES)  # the others still need them
        self.sh(f"{installed('boostdeck')} uninstall")
        self.assertEqual(self.unit.rules(), RULES)
        self.sh(f"{installed('engine-codes')} uninstall")
        self.sh(f"{installed('mhd-maps')} uninstall")
        self.sh(f"{installed('shiftdeck')} uninstall --purge")  # the last one
        for plugin in PLUGINS:
            self.assert_absent(plugin)
        self.assertEqual(self.unit.rules(), [])
        self.assertFalse(self.unit.path(DATA).exists())
        self.assertEqual(self.leftovers(), [])

    def test_update_while_idrive_uses_the_app(self):
        self.install("boostdeck")
        self.unit.hold("/opt/hmi/data/ro/Plugins/BoostDeck/libBoostDeck.so")
        out = self.install("boostdeck")
        self.assertIn("The previous version stays until the restart", out)
        self.assertEqual(self.leftovers(), [".entrynav-boostdeck.old"])
        self.assert_installed("boostdeck")
        self.assertIn("(in use until the restart)", self.sh(f"{installed('boostdeck')} status"))
        self.assertIn("Still in use", self.sh(f"{installed('boostdeck')} tidy", 1))
        self.unit.release()
        self.assertIn("Tidied.", self.sh(f"{installed('boostdeck')} tidy"))
        self.assertEqual(self.leftovers(), [])

    def test_an_interrupted_copy_is_cleaned_up(self):
        self.unit.put(f"opt/.entrynav-shiftdeck.new/{MARK}", "")
        self.assertIn("left by an earlier operation, will be deleted", self.install("shiftdeck"))
        self.assertEqual(self.leftovers(), [])

    def test_uninstall_while_idrive_uses_the_app(self):
        self.install("bm3-maps")
        self.unit.hold("/opt/hmi/data/ro/Plugins/Bm3Maps/libBm3Maps.so")
        self.assertIn("keeps using its files until the restart", self.sh(f"{installed('bm3-maps')} uninstall"))
        self.assert_absent("bm3-maps")
        self.assertEqual(self.leftovers(), [".entrynav-bm3-maps.old"])
        self.unit.release()
        self.assertIn("Tidied.", self.sh(f"sh /{DATA}/bm3-maps-setup.sh tidy"))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.unit.path(f"{DATA}/bm3-maps-setup.sh").exists())

    # ---- the gateway ---------------------------------------------------------------------------------------------
    def test_gateway_is_checked_and_saved(self):
        self.install("boostdeck")
        self.assertEqual(self.unit.sh(f"ifconfig lo:1 {GATEWAY} netmask 255.255.255.255 up").returncode, 0)
        requests = []

        def gateway(listener):
            conn, _ = listener.accept()
            with conn:
                size, _control = struct.unpack(">IH", conn.recv(6, socket.MSG_WAITALL))
                requests.append(conn.recv(size, socket.MSG_WAITALL))
                body = bytes.fromhex("12f44100be3fa813")
                conn.sendall(struct.pack(">IH", len(body), 1) + body)

        with socket.socket() as listener:
            listener.bind((GATEWAY, 6801))
            listener.listen()
            listener.settimeout(20)
            thread = threading.Thread(target=gateway, args=(listener,), daemon=True)
            thread.start()
            out = self.sh(f"{installed('boostdeck')} gateway {GATEWAY}")
            thread.join(5)
        self.assertIn("Saved for the apps", out)
        self.assertEqual(requests, [bytes.fromhex("f4120100")])
        self.assertEqual(self.unit.path(f"{DATA}/gateway-ip.txt").read_text(), GATEWAY + "\n")
        self.assertIn(f"gateway: {GATEWAY}", self.sh(f"{installed('boostdeck')} status"))

    def test_gateway_refuses_an_address_off_the_diagnostic_link(self):
        self.install("boostdeck")
        for address in ("160.48.199.97", "8.8.8.8", "172.32.0.1", "169.254.300.1", "169.254.0.5", "10.1.2", "10.01.2.3"):
            self.assertIn("can't be the gateway", self.sh(f"{installed('boostdeck')} gateway {address}", 1))

    def test_gateway_takes_an_address_from_a_laptops_dhcp_network(self):
        self.install("boostdeck")
        for address in ("10.126.134.54", "172.16.0.9", "192.168.2.3"):
            out = self.sh(f"{installed('boostdeck')} gateway {address}", 1)  # nothing answers in the container
            self.assertNotIn("can't be the gateway", out)
            self.assertIn(f"No connection to {address}", out)

    def test_install_replaces_the_rule_of_0_1_0_test_1(self):
        self.unit.sh(f"iptables -A {RULE_HSFZ_OLD}")
        self.install("shiftdeck")
        self.assertEqual(self.unit.rules(), RULES)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *sys.argv[1:]], verbosity=2)
