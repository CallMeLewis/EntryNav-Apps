"""tools/try_on_car.py against the fake head unit of tests/fake_headunit.py: the car's own shell, BusyBox and openssl,
the installed C workers, and a simulated gateway. Needs Docker, build-arm (bash toolchain/tc.sh make arm) and build/rust
(make rust):

    uv run --no-project --with paramiko tests/test_try_on_car.py

It checks that a run uploads, verifies and compares both workers, writes nothing outside its /tmp folder and deletes
that folder, also after a damaged upload or Ctrl-C; that --clear and other arguments that aren't read-only are refused
before it connects; and that it stops, having uploaded nothing, when another worker holds the diagnostic lease or is
running, when /tmp is noexec, when no gateway is saved, and when the password is wrong.
"""

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from fake_headunit import DELAY_FILE, GATEWAY, PASSWORD, REQUESTS_LOG, HeadUnitContainer, ssh_server

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "try_on_car.py"
WINDOWS = os.name == "nt"
# What the head unit's system looks like to the test: the overlay's writes (everything outside the mounts below),
# and the data partition. /tmp is the script's to use.
WRITES = "cd /ovl/up && find . | sort; cd /hu/var/local && find . -type f | sort | xargs -r md5sum"


class TryOnCarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for worker in ("engine-codes", "ena-gateway"):
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
        self.assertEqual(self.unit.unit("pidof engine-codes ena-gateway boostdeck-collector", check=False).stdout, "")

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
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn("Firmware 202306092158", out)
        self.assertIn("/tmp is writable, allows programs", out)
        self.assertIn(f"Saved gateway: {GATEWAY}", out)
        for worker in ("engine-codes", "ena-gateway"):
            size = (ROOT / "build" / "rust" / worker).stat().st_size
            self.assertRegex(out, rf"Uploaded {worker}: {size} bytes, SHA-256 [0-9a-f]{{64}} matches "
                                  r"\(openssl on the car\)")
        self.assertIn("Installed C engine-codes: /opt/hmi/data/ro/Plugins/EngineCodes/engine-codes", out)
        self.assertIn(f"== --host {GATEWAY} --codes --gateway-file /tmp/ena-rust.", out)
        self.assertIn("Engine: 2 codes", out)
        self.assertIn(f"Gateway {GATEWAY} answers; the DME replied to OBD 01 00", out)
        self.assertIn(f"     {GATEWAY}\n", out)  # discover found it on vbr0
        self.assertEqual(out.count("Result: MATCH"), 3)
        self.assertIn("3 of 3 compared runs matched.", out)
        self.assertRegex(out, r"peak memory +\d+ kB +\d+ kB")
        self.assertRegex(out, r"Deleted /tmp/ena-rust\.\w{6} on the head unit")
        # Only reads reached the engine computer: the fault memory twice and the supported PIDs twice.
        self.assertEqual(sorted(self.requests()), ["0100", "0100", "1902af", "1902af"])

    def test_one_worker_with_its_own_read_only_arguments(self):
        result = self.run_script("--worker", "ena-gateway", "--worker-args", f"check --host {GATEWAY} --port 6801")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 of 1 compared runs matched.", result.stdout)
        self.assertNotIn("Uploaded engine-codes", result.stdout)

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
                 ("ena-gateway", "discover --dev 'vbr0;reboot'", "needs a valid value")]
        for worker, args, why in cases:
            with self.subTest(args=args):
                result = self.run_script("--worker", worker, "--worker-args", args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Refused: ", result.stderr)
                self.assertIn(why, result.stderr)
        self.assertEqual(self.server.connections, self.connections, "it connected for a refused run")

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


if __name__ == "__main__":
    unittest.main()
