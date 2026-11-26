"""tools/try_on_car.py against the fake head unit of tests/fake_headunit.py: the car's own shell, BusyBox and openssl,
the installed C workers, and a simulated gateway. Needs Docker, build-arm (bash toolchain/tc.sh make arm) and build/rust
(make rust):

    uv run --no-project --with paramiko tests/test_try_on_car.py

It checks that a run uploads, verifies and compares every worker, the gauge workers by the frames ena-capture records,
writes nothing outside its /tmp folder and deletes that folder, also after a damaged upload or Ctrl-C; that only reads
reach the engine computer, that mhd-maps is left out without MHD+ Suite and sends nothing with it; that --clear, map
switching and the other arguments that aren't read-only are refused before it connects; and that it stops, having uploaded nothing,
when another worker holds the diagnostic lease or is running, when /tmp is noexec, when no gateway is saved, and when
the password is wrong, and stops its own gauge worker when an app's worker starts during the run. GaugeComparisonTest
checks, without Docker, how the gauge runs are compared and which differences count.
"""

import importlib.util
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from fake_headunit import (BM3_STATUS, DELAY_FILE, GATEWAY, PASSWORD, PLUGINS, READINGS, REQUESTS_LOG,
                           HeadUnitContainer, ssh_server)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "try_on_car.py"
WINDOWS = os.name == "nt"
# What the head unit's system looks like to the test: the overlay's writes (everything outside the mounts below),
# and the data partition. /tmp is the script's to use.
WRITES = "cd /ovl/up && find . | sort; cd /hu/var/local && find . -type f | sort | xargs -r md5sum"
RUST = ("engine-codes", "ena-gateway", "bm3-maps", "mhd-maps", "boostdeck-collector", "shift-deck", "ena-capture")
OURS = "engine-codes ena-gateway bm3-maps mhd-maps boostdeck-collector shift-deck ena-capture"
# What a request to the engine computer may start with when it only reads, or defines bootmod3's dynamic DID (2C).
WRITING = ("14", "2e", "2f", "3d", "3e")


def load_script():
    spec = importlib.util.spec_from_file_location("try_on_car", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TryOnCarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for worker in RUST:
            if not (ROOT / "build" / "rust" / worker).is_file():
                raise unittest.SkipTest(f"build/rust/{worker} is missing: make rust")
        cls.unit = HeadUnitContainer()
        try:
            cls.server, cls.stop_server = ssh_server(cls.unit)
        except BaseException:
            cls.unit.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        cls.unit.stop()

    def setUp(self):
        self.server.corrupt_uploads = False
        self.unit.sh(f"echo 0 > {DELAY_FILE}; : > {REQUESTS_LOG}")
        self.before = self.unit.sh(WRITES).stdout
        self.connections = self.server.connections

    def tearDown(self):
        self.assertEqual(self.unit.sh(WRITES).stdout, self.before, "something was written outside /tmp")
        self.assertEqual(self.folders(), "", "a folder was left in /tmp")
        self.assertEqual(self.unit.unit(f"pidof {OURS}", check=False).stdout, "")

    def folders(self):
        return self.unit.sh("ls -d /hu/tmp/ena-rust.* 2>/dev/null", check=False).stdout

    def requests(self):
        return self.unit.sh(f"cat {REQUESTS_LOG}").stdout.split()

    def run_script(self, *args, password=PASSWORD, timeout=180):
        env = dict(os.environ, HEADUNIT_PASSWORD=password, PYTHONUNBUFFERED="1")
        result = subprocess.run([sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--ssh-port",
                                 str(self.server.port), *args], capture_output=True, text=True, env=env,
                                timeout=timeout, check=False)
        self.assertNotIn(password, result.stdout + result.stderr, "the password was printed")
        return result

    def test_runs_both_builds_and_they_match(self):
        result = self.run_script("--seconds", "4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn("Firmware 202306092158", out)
        self.assertIn("/tmp is writable, allows programs", out)
        self.assertIn(f"Saved gateway: {GATEWAY}", out)
        for worker in (w for w in RUST if w != "mhd-maps"):
            size = (ROOT / "build" / "rust" / worker).stat().st_size
            self.assertRegex(out, rf"Uploaded {worker}: {size} bytes, SHA-256 [0-9a-f]{{64}} matches "
                                  r"\(openssl on the car\)")
        self.assertIn("Installed C engine-codes: /opt/hmi/data/ro/Plugins/EngineCodes/engine-codes", out)
        self.assertIn("Installed C shift-deck: /opt/hmi/data/ro/Plugins/ShiftDeck/shift-deck", out)
        self.assertIn(f"== --host {GATEWAY} --codes --gateway-file /tmp/ena-rust.", out)
        self.assertIn("Engine: 2 codes", out)
        self.assertIn(f"== --host {GATEWAY} --status --gateway-file /tmp/ena-rust.", out)
        self.assertIn("     Map 2 active\n", out)
        # The owner's car, as the pretend one, has no MHD+ Suite: mhd-maps is left out, not uploaded.
        self.assertIn("MHD+ Suite isn't installed on this car", out)
        self.assertNotIn("Uploaded mhd-maps", out)
        self.assertIn(f"Gateway {GATEWAY} answers; the DME replied to OBD 01 00", out)
        self.assertIn(f"     {GATEWAY}\n", out)  # discover found it on vbr0
        self.assertIn(f"== --host {GATEWAY} --session --udp-port PORT --gateway-file /tmp/ena-rust.", out)
        self.assertIn("Running the Rust boostdeck-collector for 4 s", out)
        self.assertRegex(out, r"Coolant +33\.2 C, 100% given +33\.2 C, 100% given")
        self.assertRegex(out, r"Engine speed +696\.0 rpm, 100% given +696\.0 rpm, 100% given")
        self.assertRegex(out, r"frames +\d+, [\d.]+ a second +\d+, [\d.]+ a second")
        self.assertEqual(out.count("Result: MATCH"), 6)
        self.assertIn("6 of 6 compared runs matched.", out)
        self.assertRegex(out, r"peak memory +\d+ kB +\d+ kB")
        self.assertRegex(out, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")
        # Only reads reached the engine computer: the fault memory twice, the supported PIDs twice, bootmod3's marker
        # and map slot twice, and the gauges' DIDs (22) and OBD speeds (01 0C, 01 0D).
        requests = self.requests()
        self.assertEqual(sorted(r for r in requests if r in ("0100", "1902af")), ["0100", "0100", "1902af", "1902af"])
        self.assertEqual([r for r in requests if r in BM3_STATUS], [*BM3_STATUS] * 2)
        self.assertTrue(set(requests) - {"0100", "1902af"} <= set(READINGS) | set(BM3_STATUS), set(requests))
        self.assertFalse([r for r in requests if r.startswith(WRITING)])
        self.assertGreater(len(requests), 100)

    def test_the_gauge_workers_alone(self):
        result = self.run_script("--worker", "gauges", "--seconds", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn("Uploaded ena-capture", out)
        self.assertNotIn("Uploaded engine-codes", out)
        self.assertIn("Running the C shift-deck for 3 s", out)
        self.assertIn("2 of 2 compared runs matched.", out)
        self.assertIn("Result: MATCH, the same readings, frame rates within 15% and medians within tolerance", out)

    def test_one_worker_with_its_own_read_only_arguments(self):
        result = self.run_script("--worker", "ena-gateway", "--worker-args", f"check --host {GATEWAY} --port 6801")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 of 1 compared runs matched.", result.stdout)
        self.assertNotIn("Uploaded engine-codes", result.stdout)
        result = self.run_script("--worker", "boostdeck-collector", "--seconds", "3", "--worker-args",
                                 f"--host {GATEWAY} --session --no-trans")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Transmission   never given", result.stdout)
        self.assertIn("1 of 1 compared runs matched.", result.stdout)
        self.assertNotIn("22da12", self.requests())

    def test_refuses_arguments_that_are_not_read_only_before_connecting(self):
        cases = [("engine-codes", f"--host {GATEWAY} --clear", "--clear clears the engine computer's fault memory"),
                 ("engine-codes", f"--clear --host {GATEWAY}", "--clear clears"),
                 ("engine-codes", f"--host {GATEWAY} --codes --clear", "--clear clears"),
                 ("engine-codes", f"--host {GATEWAY}", "needs --codes"),
                 ("engine-codes", f"--host {GATEWAY} --codes --gateway-file /var/local/data/x", "isn't a read-only"),
                 ("engine-codes", f"--host {GATEWAY} --codes --search-dev vbr0", "isn't a read-only"),
                 ("engine-codes", f"--host {GATEWAY} --codes --codes", "given twice"),
                 ("engine-codes", "--host 1.2.3.4;reboot --codes", "needs a valid value"),
                 ("ena-gateway", "scan", "takes discover or check"),
                 ("ena-gateway", "check", "needs --host"),
                 ("ena-gateway", "discover --dev 'vbr0;reboot'", "needs a valid value"),
                 ("boostdeck-collector", f"--host {GATEWAY}", "needs --session"),
                 ("boostdeck-collector", "--session --no-trans", "needs --host"),
                 ("boostdeck-collector", f"--host {GATEWAY} --session --demo", "isn't a read-only"),
                 ("boostdeck-collector", f"--host {GATEWAY} --session --udp-port 42860", "isn't a read-only"),
                 ("boostdeck-collector", f"--host {GATEWAY} --session --cycles 5", "isn't a read-only"),
                 ("boostdeck-collector", f"--host {GATEWAY} --session --session", "given twice"),
                 ("shift-deck", f"--host {GATEWAY} --gateway-file /var/local/data/x", "isn't a read-only"),
                 ("shift-deck", f"--host {GATEWAY} --session", "isn't a read-only"),
                 ("shift-deck", "--host 1.2.3.4;reboot", "needs a valid value"),
                 ("bm3-maps", f"--host {GATEWAY} --map 2", "--map writes bootmod3's map slot"),
                 ("bm3-maps", f"--host {GATEWAY} --status --map 2", "--map writes"),
                 ("bm3-maps", f"--map 1 --host {GATEWAY}", "--map writes"),
                 ("bm3-maps", f"--host {GATEWAY} --burbles 0", "--burbles writes the burble table"),
                 ("bm3-maps", f"--host {GATEWAY} --sound 1", "--sound sends the engine computer"),
                 ("bm3-maps", f"--host {GATEWAY} --status --sound 0", "--sound sends"),
                 ("bm3-maps", f"--host {GATEWAY}", "needs --status"),
                 ("bm3-maps", f"--host {GATEWAY} --status --marker 0977FE4", "needs a valid value"),
                 ("bm3-maps", f"--host {GATEWAY} --status --marker '0977FE4C;reboot'", "needs a valid value"),
                 ("bm3-maps", f"--host {GATEWAY} --status --search-dev lo", "isn't a read-only"),
                 ("bm3-maps", f"--host {GATEWAY} --status --status", "given twice"),
                 ("mhd-maps", f"--host {GATEWAY} --map 1", "--map asks the engine computer's MHD code"),
                 ("mhd-maps", f"--host {GATEWAY} --cooling 0", "--cooling sends"),
                 ("mhd-maps", f"--cooling 1 --host {GATEWAY}", "--cooling sends"),
                 ("mhd-maps", f"--host {GATEWAY} --status", "isn't a read-only")]
        for worker, args, why in cases:
            with self.subTest(args=args):
                result = self.run_script("--worker", worker, "--worker-args", args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Refused: ", result.stderr)
                self.assertIn(why, result.stderr)
        self.assertEqual(self.server.connections, self.connections, "it connected for a refused run")

    def test_bm3_maps_status_with_its_dump(self):
        result = self.run_script("--worker", "bm3-maps", "--worker-args", f"--host {GATEWAY} --status --dump")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Installed C bm3-maps: /opt/hmi/data/ro/Plugins/Bm3Maps/bm3-maps", result.stdout)
        self.assertIn("1 of 1 compared runs matched.", result.stdout)
        self.assertEqual(self.requests(), [*BM3_STATUS] * 2)

    def test_mhd_maps_is_left_out_without_mhd_plus(self):
        result = self.run_script("--worker", "mhd-maps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MHD+ Suite isn't installed on this car, so there is no C mhd-maps to compare with", result.stdout)
        self.assertIn("Nothing to compare.", result.stdout)
        self.assertNotIn("Made /tmp", result.stdout)
        self.assertEqual(self.requests(), [])

    def test_mhd_maps_with_mhd_plus_installed_sends_nothing(self):
        folder = f"{PLUGINS}/MhdMaps"
        self.unit.sh(f"mkdir {folder} && cp /work/build-arm/mhd-maps {folder}/ && chmod 755 {folder}/mhd-maps")
        try:
            result = self.run_script("--worker", "mhd-maps")
        finally:
            self.unit.sh(f"rm -rf {folder}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Installed C mhd-maps: /opt/hmi/data/ro/Plugins/MhdMaps/mhd-maps", result.stdout)
        self.assertIn(f"== --host {GATEWAY} --gateway-file /tmp/ena-rust.", result.stdout)
        self.assertIn("exit code      2", result.stdout)
        self.assertIn("1 of 1 compared runs matched.", result.stdout)
        self.assertEqual(self.requests(), [])

    def test_stops_when_another_worker_holds_the_lease(self):
        self.unit.sh("python3 -c 'import socket, time; s = socket.socket(2, 2); s.bind((\"127.0.0.1\", 42861)); "
                     "time.sleep(120)' & echo $! > /var/tmp/holder")
        try:
            time.sleep(0.5)
            result = self.run_script()
        finally:
            self.unit.sh("kill $(cat /var/tmp/holder)")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("holds the diagnostic lease (UDP 127.0.0.1:42861)", result.stderr)
        self.assertIn("Nothing was uploaded", result.stderr)
        self.assertNotIn("Made /tmp", result.stdout)
        self.assertEqual(self.requests(), [])

    def test_stops_when_a_gauge_worker_is_running(self):
        self.unit.unit("/run/boostdeck-collector --demo --cycles 1000000", detach=True)
        try:
            time.sleep(1)
            result = self.run_script()
        finally:
            self.unit.unit("kill $(pidof boostdeck-collector)", check=False)
            time.sleep(0.5)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("An EntryNav-Apps worker is running (boostdeck-collector", result.stderr)
        self.assertNotIn("Made /tmp", result.stdout)

    def test_a_damaged_upload_is_caught_and_cleaned_up(self):
        self.server.corrupt_uploads = True
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("the upload is damaged", result.stderr)
        self.assertRegex(result.stdout, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")
        self.assertEqual(self.requests(), [])  # nothing ran

    def test_ctrl_c_during_a_run_stops_the_worker_and_cleans_up(self):
        env = dict(os.environ, HEADUNIT_PASSWORD=PASSWORD, PYTHONUNBUFFERED="1")
        # A search that waits 10 s: Ctrl-C comes while the Rust worker runs.
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--ssh-port",
                                    str(self.server.port), "--worker", "ena-gateway", "--worker-args",
                                    "discover --wait 10000"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
        seen = []
        for line in process.stdout:
            seen.append(line)
            if line.startswith("Uploaded ena-gateway"):
                break
        running = ""
        deadline = time.monotonic() + 20
        while not running and time.monotonic() < deadline:
            time.sleep(0.2)
            running = self.unit.unit("pidof ena-gateway", check=False).stdout.strip()
        self.assertTrue(running, "the worker never started")
        started = time.monotonic()
        process.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        out, err = process.communicate(timeout=60)
        out = "".join(seen) + out
        self.assertEqual(process.returncode, 130, out + err)
        self.assertLess(time.monotonic() - started, 8, "it waited for the run to end instead of stopping it")
        self.assertIn("Interrupted. Cleaning up", err)
        self.assertRegex(out, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")
        self.assertNotIn("Result:", out)

    def start_script(self, *args):
        """The script, running in the background."""
        env = dict(os.environ, HEADUNIT_PASSWORD=PASSWORD, PYTHONUNBUFFERED="1")
        return subprocess.Popen([sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--ssh-port",
                                 str(self.server.port), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)

    def read_until(self, process, start):
        seen = []
        for line in process.stdout:
            seen.append(line)
            if line.startswith(start):
                break
        return seen

    def wait_for(self, names):
        """The pids of the named programs on the head unit, once all of them run."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found = [self.unit.unit(f"pidof {name}", check=False).stdout.strip() for name in names]
            if all(found):
                return found
            time.sleep(0.2)
        self.fail(f"{names} never all ran")

    def test_stops_its_gauge_worker_when_an_apps_worker_starts(self):
        process = self.start_script("--worker", "shift-deck", "--seconds", "20")
        seen = self.read_until(process, "Running the Rust shift-deck")
        self.wait_for(["shift-deck", "ena-capture"])
        started = time.monotonic()
        self.unit.unit("/run/boostdeck-collector --demo --cycles 1000000", detach=True)  # an app's page opened
        try:
            out, err = process.communicate(timeout=60)
        finally:
            self.unit.unit("kill $(pidof boostdeck-collector)", check=False)
            time.sleep(0.5)
        out = "".join(seen) + out
        self.assertEqual(process.returncode, 3, out + err)
        self.assertLess(time.monotonic() - started, 10, "it ran on beside the app's worker")
        self.assertIn("An app's worker started during the run (boostdeck-collector:", err)
        self.assertRegex(out, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")
        self.assertNotIn("Result:", out)

    def test_ctrl_c_during_a_gauge_run_stops_the_worker_and_the_capture(self):
        process = self.start_script("--worker", "boostdeck-collector", "--seconds", "60")
        seen = self.read_until(process, "Running the Rust boostdeck-collector")
        self.wait_for(["boostdeck-collector", "ena-capture"])
        started = time.monotonic()
        process.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        out, err = process.communicate(timeout=60)
        out = "".join(seen) + out
        self.assertEqual(process.returncode, 130, out + err)
        self.assertLess(time.monotonic() - started, 8, "it waited for the run to end instead of stopping it")
        self.assertIn("Interrupted. Cleaning up", err)
        self.assertRegex(out, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")

    def test_stops_when_tmp_is_noexec(self):
        self.unit.sh("mount -o remount,noexec /hu/tmp")
        try:
            result = self.run_script()
        finally:
            self.unit.sh("mount -o remount,exec /hu/tmp")
        self.assertEqual(result.returncode, 3)
        self.assertIn("/tmp is mounted noexec", result.stderr)

    def test_stops_without_a_saved_gateway(self):
        data = "/hu/var/local/data/EntryNavApps"
        self.unit.sh(f"mv {data}/gateway-ip.txt /var/tmp/saved-gateway")
        try:
            result = self.run_script("--worker", "engine-codes")
        finally:
            self.unit.sh(f"mv /var/tmp/saved-gateway {data}/gateway-ip.txt")
        self.assertEqual(result.returncode, 3)
        self.assertIn("No usable gateway address", result.stderr)
        self.assertNotIn("Made /tmp", result.stdout)

    def test_a_wrong_password(self):
        result = self.run_script(password="not-the-password")
        self.assertEqual(result.returncode, 3)
        self.assertIn("refused the password", result.stderr)


class GaugeComparisonTest(unittest.TestCase):
    """How the script compares two gauge runs, from made-up captures: what counts as the same and what differs."""

    BANNER = b"READ ONLY / HSFZ: 2016 340i candidate profile; validate against ISTA/logger.\n"
    IDLE = [-9.8, 14.7, 7.8, 49.5, 94.0, 89.14, 2.497, 33.25]

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def boost(self, frames=500, gap=35, values=None, valid=255, stderr=BANNER, code=0, skew=0, start=5_000_000):
        """A BoostDeck run: frames every gap ms, each reading at values (or idle)."""
        captured = []
        for n in range(frames):
            sent = start + n * gap
            text = " ".join([f"NBG3 {n + 1} 0 {valid} {sent}"] + [f"{v:.6f} 0" for v in values or self.IDLE]) + "\n"
            captured.append((sent + skew, text.encode()))
        return {"code": code, "stderr": stderr, "frames": captured}

    def shift(self, frames=800, gap=22, rpm=696, kph=0):
        captured = [(5_000_000 + n * gap, (5_000_000 + n * gap).to_bytes(8, "little") + rpm.to_bytes(4, "little")
                     + kph.to_bytes(4, "little", signed=True)) for n in range(frames)]
        return {"code": 0, "stderr": b"", "frames": captured}

    def problems(self, worker, rust, c):
        return self.script.gauge_problems(worker, rust, c)

    def test_the_same_run_matches(self):
        self.assertEqual(self.problems("boostdeck-collector", self.boost(), self.boost()), [])
        self.assertEqual(self.problems("shift-deck", self.shift(), self.shift()), [])

    def test_live_readings_may_drift_within_their_tolerance(self):
        drifted = [-8.0, 15.5, 1.0, 52.0, 96.0, 110.0, 2.2, 36.0]
        self.assertEqual(self.problems("boostdeck-collector", self.boost(values=drifted, gap=38), self.boost()), [])
        self.assertEqual(self.problems("shift-deck", self.shift(rpm=850, kph=2), self.shift()), [])

    def test_a_slower_frame_rate_differs(self):
        found = self.problems("boostdeck-collector", self.boost(gap=45), self.boost())
        self.assertEqual(len(found), 1, found)
        self.assertIn("22.2 frames a second against the C's 28.6, more than 15% apart", found[0])

    def test_a_reading_missing_or_far_off_differs(self):
        self.assertEqual(self.problems("boostdeck-collector", self.boost(valid=254), self.boost()),
                         ["Boost in 0% of the Rust frames and 100% of the C's"])
        found = self.problems("boostdeck-collector", self.boost(values=[*self.IDLE[:7], 45.0]), self.boost())
        self.assertEqual(found, ["Coolant: median 45.0 against the C's 33.2, more than 5 C apart"])
        self.assertEqual(self.problems("shift-deck", self.shift(rpm=1200), self.shift()),
                         ["Engine speed: median 1200.0 against the C's 696.0, more than 250 rpm apart"])
        self.assertEqual(self.problems("shift-deck", self.shift(kph=-1), self.shift()),
                         ["Road speed in 0% of the Rust frames and 100% of the C's"])

    def test_faults_in_the_frames_and_how_it_ended(self):
        off_clock = self.boost(skew=2000)
        self.assertIn("the Rust worker's frames: 500 stamped off the monotonic clock",
                      self.problems("boostdeck-collector", off_clock, self.boost()))
        broken = self.boost()
        broken["frames"][10:12] = [(broken["frames"][10][0], b"NBG3 1 2\n")]
        self.assertIn("the C worker's frames: 1 unreadable, 1 out of order",
                      self.problems("boostdeck-collector", self.boost(), broken))
        self.assertIn("the Rust worker exited 143, not 0 after SIGTERM",
                      self.problems("boostdeck-collector", self.boost(code=143), self.boost()))
        self.assertIn("the Rust worker sent no frames", self.problems("shift-deck", self.shift(frames=0), self.shift()))
        short = self.shift()
        short["frames"][5] = (short["frames"][5][0], b"\0" * 15)
        self.assertIn("the C worker's frames: 1 unreadable", self.problems("shift-deck", self.shift(), short))

    def test_errors_on_stderr_must_be_the_same(self):
        refused = self.boost(stderr=self.BANNER + b"ECU 12 DID 4205 NRC 31\n")
        self.assertEqual(self.problems("boostdeck-collector", refused, self.boost()), ["different errors on stderr"])
        self.assertEqual(self.problems("boostdeck-collector", refused, refused), [])
        moved = self.boost(stderr=self.BANNER + b"The gateway moved from 169.254.10.3 to 169.254.10.4\n")
        self.assertEqual(self.problems("boostdeck-collector", moved, self.boost()), [])
        self.assertEqual(self.problems("shift-deck", {**self.shift(), "stderr": b"x\n"}, self.shift()),
                         ["different errors on stderr"])

    def test_the_pace_counts_stalls(self):
        self.assertEqual(self.script.pace([0, 30, 60, 700, 730]), (4 * 1000 / 730, 640, 1))
        self.assertEqual(self.script.pace([100]), (0.0, 0, 0))

    def test_captured_lines(self):
        self.assertEqual(self.script.captured(b"PORT 40000\n123 4e42\nbad line\n124 \n"),
                         [(123, b"NB"), (124, b"")])


if __name__ == "__main__":
    unittest.main()
