r"""A pretend EntryNav2 that tools/try_on_car.py can log in to, made from the copy of the car's system. Local only.

    uv run --no-project --with paramiko tests/fake_headunit.py [--port 2222]

then, in another terminal (the password is fixture-only):

    HEADUNIT_PASSWORD=fixture-only uv run --no-project --with paramiko tools/try_on_car.py \
        --host 127.0.0.1 --ssh-port 2222

The head unit is a disposable aa-entrynav-qemu:local container, started with --privileged, holding the copy of the car's
system from the Docker volume entrynav2-rootfs (HU_ROOTFS to use another) read-only under an overlay, entered with
chroot. Every command runs with the car's own bash 3.2, BusyBox 1.21 and openssl, and its ARM programs run through the
kernel's binfmt registration for QEMU. As on the car, /tmp is a tmpfs that allows programs, /var/local a noexec tmpfs
with the apps' saved gateway, and /opt/hmi/data/ro/Plugins holds the C engine-codes and ena-gateway (EngineCodes),
boostdeck-collector (BoostDeck), shift-deck (ShiftDeck) and bm3-maps (Bm3Maps) from build-arm. MHD+ Suite isn't
installed, as in the owner's car. A dummy vbr0 carries the head unit's address and the gateway's, 169.254.10.2 and
169.254.10.3, where a simulated gateway answers HSFZ on TCP 6801 (the fault codes, the supported PIDs, the gauges'
readings, and a DME running bootmod3's R1C900000 with map 2 active) and vehicle identification on UDP 6811.

The SSH server runs on this computer (paramiko) and passes each command to the container's chroot. It takes root with
the password fixture-only. tests/test_try_on_car.py uses it to test the trial script.
"""

import argparse
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "fixture-only"
HEAD_UNIT, GATEWAY = "169.254.10.2", "169.254.10.3"
PLUGINS = "/hu/opt/hmi/data/ro/Plugins"
# Which C workers the head unit has installed, and where: build-arm's, in each app's folder.
INSTALLED = {"EngineCodes": ("engine-codes", "ena-gateway"), "BoostDeck": ("boostdeck-collector",),
             "ShiftDeck": ("shift-deck",), "Bm3Maps": ("bm3-maps",)}
INSTALL = "\n".join(f"mkdir -p {PLUGINS}/{app} && cp " + " ".join(f"/work/build-arm/{w}" for w in workers)
                    + f" {PLUGINS}/{app}/ && chmod 755 " + " ".join(f"{PLUGINS}/{app}/{w}" for w in workers)
                    for app, workers in INSTALLED.items())
# Inside the container: what the gateway was asked, and how long it waits before each answer (seconds, a file).
REQUESTS_LOG, DELAY_FILE = "/var/tmp/gateway-requests", "/var/tmp/gateway-delay"
# How a command runs on the head unit: in the chroot, with the environment an SSH login gives root.
UNIT_SHELL = ("chroot", "/hu", "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root",
              "/bin/sh", "-c")

SETUP = f"""
set -e
mkdir -p /ovl /hu
mount -t tmpfs tmpfs /ovl
mkdir -p /ovl/up /ovl/work
mount -t overlay overlay -o lowerdir=/sysroot,upperdir=/ovl/up,workdir=/ovl/work /hu
for d in proc dev tmp run var/local root; do mkdir -p /hu/$d; done
mount -t proc proc /hu/proc
mount --bind /dev /hu/dev
mount -t tmpfs tmpfs /hu/tmp
mount -t tmpfs tmpfs /hu/run
mount -t tmpfs -o noexec tmpfs /hu/var/local
mkdir -p /hu/var/local/data/EntryNavApps
{INSTALL}
echo {GATEWAY} > /hu/var/local/data/EntryNavApps/gateway-ip.txt
cp /work/build-arm/boostdeck-collector /hu/run/boostdeck-collector
chroot /hu /bin/sh -c 'ip link add vbr0 type dummy && ip addr add {HEAD_UNIT}/16 dev vbr0 &&
    ip addr add {GATEWAY}/16 dev vbr0 && ip link set vbr0 up'
echo 0 > {DELAY_FILE}
: > {REQUESTS_LOG}
"""


# ---- the simulated gateway, run inside the container ---------------------------------------------------------------

# BoostDeck's DIDs (22) as a 2016 340i answers them, standing with the engine at idle, and ShiftDeck's OBD speeds (01).
READINGS = {"224201": "63ca", "224205": "31e5", "225889": "104e", "224a49": "004e", "22580f": "82", "22da12": "008e",
            "2256d7": "1169", "22586f": "09c1", "224300": "0bf8", "010c": "0ae0", "010d": "00"}
# What bm3-maps --status reads from a DME running bootmod3's R1C900000 (src/bm3_profiles.h): its CustomROM marker in the
# text layout, through the dynamic DID F301, and map slot 2 (byte 01) at 4005E006.
BM3_STATUS = {"2c03f301": "6c03f301", "2c02f30124807ffe5000ff": "6c02f301",
              "22f301": "62f301" + b"BM3_R#R1C900000#".ljust(255, b"\0").hex(), "23144005e00601": "6301"}


def answer(uds):
    """The answer to one UDS request: two fault codes, the supported PIDs, the gauges' readings, bootmod3's marker and
    map slot, and a refusal of anything else."""
    if uds == bytes.fromhex("1902af"):
        return bytes.fromhex("5902ff" "1a2f0f2f" "30fe0108")
    if uds == bytes.fromhex("0100"):
        return bytes.fromhex("4100be3fa813")
    if uds.hex() in BM3_STATUS:
        return bytes.fromhex(BM3_STATUS[uds.hex()])
    if uds.hex() in READINGS:
        return bytes([uds[0] + 0x40]) + uds[1:] + bytes.fromhex(READINGS[uds.hex()])
    return bytes([0x7f, uds[0] if uds else 0, 0x11])


def serve_hsfz(conn):
    with conn:
        while True:
            header = conn.recv(6, socket.MSG_WAITALL)
            if len(header) < 6:
                return
            size, control = struct.unpack(">IH", header)
            body = conn.recv(size, socket.MSG_WAITALL) if size else b""
            if control != 1 or len(body) < 3:
                continue
            with open(REQUESTS_LOG, "a") as log:
                log.write(body[2:].hex() + "\n")
            time.sleep(float(Path(DELAY_FILE).read_text() or 0))
            reply = bytes([body[1], body[0]]) + answer(body[2:])
            conn.sendall(struct.pack(">IH", len(reply), 1) + reply)


def gateway():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((GATEWAY, 6801))
    listener.listen()
    identify = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    identify.bind(("0.0.0.0", 6811))
    reply_from = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply_from.bind((GATEWAY, 0))

    def identification():
        text = b"DIAGADR10BMWMAC001122334455BMWVINWBA00000000000000"
        while True:
            data, sender = identify.recvfrom(64)
            if data == bytes.fromhex("000000000011"):
                reply_from.sendto(struct.pack(">IH", len(text), 0x11) + text, sender)

    threading.Thread(target=identification, daemon=True).start()
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=serve_hsfz, args=(conn,), daemon=True).start()


# ---- the container ---------------------------------------------------------------------------------------------------

def docker(*args, check=True, **kwargs):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True,
                          env=dict(os.environ, MSYS_NO_PATHCONV="1"), **kwargs)


class HeadUnitContainer:
    def __init__(self):
        for worker in ("engine-codes", "ena-gateway", "boostdeck-collector", "shift-deck", "bm3-maps", "mhd-maps"):
            if not (ROOT / "build-arm" / worker).is_file():
                raise SystemExit(f"build-arm/{worker} is missing: bash toolchain/tc.sh make arm")
        self.name = f"ena-fake-headunit-{secrets.token_hex(4)}"
        docker("run", "-d", "--rm", "--privileged", "--name", self.name,
               "-v", f"{os.environ.get('HU_ROOTFS', 'entrynav2-rootfs')}:/sysroot:ro", "-v", f"{ROOT}:/work:ro",
               "aa-entrynav-qemu:local", "sleep", "infinity")
        try:
            self.sh(SETUP)
            docker("exec", "-d", self.name, "python3", "-B", "/work/tests/fake_headunit.py", "--gateway")
            for _ in range(50):
                if self.sh(f"python3 -c 'import socket; socket.create_connection((\"{GATEWAY}\", 6801), 1)'",
                           check=False).returncode == 0:
                    break
                time.sleep(0.1)
        except BaseException:
            self.stop()
            raise

    def sh(self, script, check=True):
        """A command in the container, outside the head unit."""
        return docker("exec", self.name, "sh", "-c", script, check=check)

    def unit(self, script, check=True, detach=False):
        """A command on the head unit, as its shell runs it; with detach, left running."""
        return docker("exec", *(["-d"] if detach else []), self.name, *UNIT_SHELL, script, check=check)

    def stop(self):
        docker("rm", "-f", self.name, check=False)


# ---- the SSH server --------------------------------------------------------------------------------------------------

def ssh_server(container, port=0):
    """Starts the SSH server on 127.0.0.1:port in a thread. Returns the server (with .port, .connections, .commands and
    .corrupt_uploads) and a function that stops it."""
    import paramiko

    key = paramiko.RSAKey.generate(2048)

    State = types.SimpleNamespace(port=0, connections=0, commands=[], corrupt_uploads=False)

    class Session(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            ok = username == "root" and password == PASSWORD
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            State.commands.append(command.decode())
            threading.Thread(target=self.execute, args=(channel, command.decode()), daemon=True).start()
            return True

        @staticmethod
        def execute(channel, command):
            process = subprocess.Popen(
                ["docker", "exec", "-i", container.name, *UNIT_SHELL, command],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, MSYS_NO_PATHCONV="1"))
            upload = command.startswith("cat > ")

            def feed():
                first = True
                with process.stdin:
                    try:
                        while True:
                            data = channel.recv(65536)
                            if not data:
                                break
                            if upload and first and State.corrupt_uploads:
                                data = bytes([data[0] ^ 0xff]) + data[1:]
                            first = False
                            process.stdin.write(data)
                    except OSError:
                        pass

            def pump(stream, send):
                with stream:
                    try:
                        for data in iter(lambda: stream.read1(65536), b""):
                            send(data)
                    except OSError:  # the client went away
                        pass

            threads = [threading.Thread(target=feed, daemon=True),
                       threading.Thread(target=pump, args=(process.stdout, channel.sendall), daemon=True),
                       threading.Thread(target=pump, args=(process.stderr, channel.sendall_stderr), daemon=True)]
            for thread in threads:
                thread.start()
            code = process.wait()
            for thread in threads[1:]:
                thread.join(10)
            try:
                channel.send_exit_status(code)
                channel.close()
            except OSError:
                pass

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    listener.settimeout(0.2)
    State.port = listener.getsockname()[1]
    stopping = threading.Event()
    transports = []

    def accept():
        while not stopping.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            State.connections += 1
            transport = paramiko.Transport(client)
            transport.add_server_key(key)
            transports.append(transport)
            try:
                transport.start_server(server=Session())
            except (paramiko.SSHException, EOFError, OSError):
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()

    def stop():
        stopping.set()
        thread.join(5)
        for transport in transports:
            transport.close()
        listener.close()

    return State, stop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--gateway", action="store_true", help=argparse.SUPPRESS)  # inside the container
    options = parser.parse_args()
    if options.gateway:
        gateway()
        return
    container = HeadUnitContainer()
    server, stop = None, None
    try:
        server, stop = ssh_server(container, options.port)
        print(f"Fake head unit ready on 127.0.0.1:{server.port}, user root, password {PASSWORD}. Ctrl-C stops it.",
              flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if stop:
            stop()
        container.stop()


if __name__ == "__main__":
    sys.exit(main())
