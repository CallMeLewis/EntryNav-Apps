#!/usr/bin/env python3
"""The checks of the Rust ports of ena-gateway, engine-codes, boostdeck-collector and shift-deck (rust/), against
their C originals, under QEMU with the car's kernel release:

1. build/rust's binaries are what the head unit runs: 32-bit ARM, EABI 5 hard-float, statically linked.
2. The Rust unit tests, built for the head unit's target and run under qemu-arm with no copy of the car's system.
3. tests/test_engine_codes.py, tests/test_gateway_tool.py, tests/test_transport.py (the BoostDeck collector) and
   tests/test_drive_apps.py (ShiftDeck's worker) against the ARM builds of each, C and Rust.
4. tests/test_worker_equivalence.py and tests/test_gauge_equivalence.py: every scenario on both builds, compared.

Build both first, then run it from the repository:

    make rust                                                    (from WSL on Windows: make rust CARGO=cargo.exe)
    bash toolchain/tc.sh make arm
    python3 tests/run_rust.py

Steps 2 to 4 run in the disposable aa-entrynav-qemu:local container, with the copy of the car's system from the Docker
volume entrynav2-rootfs (HU_ROOTFS to use another), read-only. cargo (CARGO to name another) builds the unit tests.
"""

import json
import os
import shlex
import shutil
import struct
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ("ena-gateway", "engine-codes", "boostdeck-collector", "shift-deck")
TARGET = "armv7-unknown-linux-musleabihf"
QEMU = "qemu-arm -cpu cortex-a15 -r 3.12.25-FR21.0"  # the car's CPU and kernel release, for uname
BUILDS = "c=build-arm,rust=build/rust"
PT_INTERP, PT_DYNAMIC = 3, 2
EM_ARM = 40
EF_ARM_EABI_VER5, EF_ARM_ABI_FLOAT_HARD = 0x05000000, 0x400


def fail(message):
    raise SystemExit(f"FAIL {message}")


def static_arm(path: Path):
    """Why path is not a static ARM EABI 5 hard-float executable, or None."""
    data = path.read_bytes()
    if data[:7] != b"\x7fELF\x01\x01\x01":
        return "not a 32-bit little-endian ELF"
    machine, = struct.unpack_from("<H", data, 18)
    flags, = struct.unpack_from("<I", data, 36)
    if machine != EM_ARM:
        return f"machine {machine}, not ARM"
    if flags & 0xff000000 != EF_ARM_EABI_VER5 or not flags & EF_ARM_ABI_FLOAT_HARD:
        return f"flags {flags:#x}, not EABI 5 hard-float"
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    for i in range(phnum):
        kind, = struct.unpack_from("<I", data, phoff + i * phentsize)
        if kind in (PT_INTERP, PT_DYNAMIC):
            return "dynamically linked: it would need the car's loader and C library"
    return None


def unit_tests():
    """Builds the unit tests for the head unit; their paths inside the container."""
    cargo = os.environ.get("CARGO") or shutil.which("cargo") or "cargo"
    built = subprocess.run([cargo, "test", "--no-run", "--locked", "--target", TARGET, "--message-format=json"],
                           cwd=ROOT / "rust", stdout=subprocess.PIPE, check=True, text=True).stdout
    paths = []
    for line in built.splitlines():
        message = json.loads(line)
        if message.get("reason") == "compiler-artifact" and message.get("executable") and message["profile"]["test"]:
            paths.append(PurePosixPath("/work", Path(message["executable"]).resolve().relative_to(ROOT).as_posix()))
    if not paths:
        fail("cargo built no unit tests")
    return paths


def main():
    for worker in (*WORKERS, "ena-capture"):  # ena-capture has no C original
        for folder in ("build/rust", "build-arm") if worker in WORKERS else ("build/rust",):
            if not (ROOT / folder / worker).is_file():
                fail(f"{folder}/{worker} is missing: build it first (see tests/run_rust.py)")
        why = static_arm(ROOT / "build/rust" / worker)
        if why:
            fail(f"build/rust/{worker}: {why}")
        size = (ROOT / "build/rust" / worker).stat().st_size
        print(f"PASS build/rust/{worker}: static ARM, EABI 5 hard-float, {size} bytes", flush=True)
    tests = unit_tests()
    steps = [f"echo '== unit tests: {test.name}' && {QEMU} {test} --test-threads=1 -q" for test in tests]
    steps += [f"echo '== {suite}' && python3 {suite}.py" for suite in
              ("test_engine_codes", "test_gateway_tool", "test_transport", "test_drive_apps",
               "test_worker_equivalence", "test_gauge_equivalence")]
    command = ["docker", "run", "--rm", "-v", f"{ROOT}:/work:ro",
               "-v", f"{os.environ.get('HU_ROOTFS', 'entrynav2-rootfs')}:/sysroot:ro",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", f"ENA_WORKERS={BUILDS}",
               "-e", f"ENA_WORKER_RUNNER={QEMU} -L /sysroot", "-w", "/work/tests",
               "aa-entrynav-qemu:local", "sh", "-ec", " && ".join(steps)]
    print("+", shlex.join(command[:3]), "... aa-entrynav-qemu:local", flush=True)
    result = subprocess.run(command, env=dict(os.environ, MSYS_NO_PATHCONV="1"), check=False)
    if result.returncode:
        fail(f"the checks under QEMU (exit {result.returncode})")
    print("PASS every Rust check")


if __name__ == "__main__":
    main()
