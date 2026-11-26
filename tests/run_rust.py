#!/usr/bin/env python3
"""The checks of the Rust ports of the six workers (rust/): ena-gateway, engine-codes, boostdeck-collector,
shift-deck, bm3-maps and mhd-maps, against their C originals, under QEMU with the car's kernel release:

1. build/rust's programs are what the head unit runs: 32-bit ARM, EABI 5 hard-float, statically linked.
2. The Rust unit tests, built for the head unit's target and run under qemu-arm with no copy of the car's system.
3. The worker tests against the ARM builds of each, C and Rust: tests/test_engine_codes.py,
   tests/test_gateway_tool.py, tests/test_boostdeck_worker.py, tests/test_shiftdeck_worker.py,
   tests/test_bm3_worker.py (bm3-maps, and test-bm3: tests/test_bm3.c and its Rust port) and
   tests/test_mhd_worker.py.
4. tests/test_worker_equivalence.py and tests/test_gauge_equivalence.py: every scenario on both builds, compared, and
   the C compared with what tests/fixtures recorded of it.

Build both first, then run it from the repository:

    make rust                                                    (from WSL on Windows: make rust CARGO=cargo.exe)
    bash toolchain/tc.sh make arm
    python3 tests/run_rust.py [--fixtures]

--fixtures compares the Rust builds with the recorded C (tests/record_fixtures.py) instead of the live C. A worker whose
C build isn't in build-arm, because its C was deleted, is checked that way by itself: its worker tests run on the Rust
build alone, and the comparisons use the recording. Steps 2 to 4 run in the disposable aa-entrynav-qemu:local
container, with the copy of the car's system from the Docker volume entrynav2-rootfs (HU_ROOTFS to use another),
read-only. cargo (CARGO to name another) builds the unit tests.
"""

import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ("ena-gateway", "engine-codes", "boostdeck-collector", "shift-deck", "bm3-maps", "mhd-maps")
# Built with them, with no C worker of the same name to compare: the car trial's recorder, and tests/test_bm3.c's port.
OTHERS = ("ena-capture", "test-bm3")
TARGET = "armv7-unknown-linux-musleabihf"
QEMU = "qemu-arm -cpu cortex-a15 -r 3.12.25-FR21.0"  # the car's CPU and kernel release, for uname
BUILDS = "c=build-arm,rust=build/rust"
# Each suite, and the programs it runs from each build. The equivalence tests choose for each worker by themselves.
SUITES = {"test_engine_codes": ("engine-codes",), "test_gateway_tool": ("ena-gateway",),
          "test_boostdeck_worker": ("boostdeck-collector",), "test_shiftdeck_worker": ("shift-deck",),
          "test_bm3_worker": ("bm3-maps", "test-bm3"), "test_mhd_worker": ("mhd-maps",),
          "test_worker_equivalence": (), "test_gauge_equivalence": ()}
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


def main(args):
    if args not in ([], ["--fixtures"]):
        raise SystemExit(__doc__)
    no_c = set()
    for program in (*WORKERS, *OTHERS):
        if not (ROOT / "build/rust" / program).is_file():
            fail(f"build/rust/{program} is missing: build it first (see tests/run_rust.py)")
        if (program in WORKERS or program == "test-bm3") and not (ROOT / "build-arm" / program).is_file():
            no_c.add(program)
            print(f"NOTE build-arm/{program} is missing: it is checked against tests/fixtures, the recorded C. "
                  "While the C exists, build it first: bash toolchain/tc.sh make arm", flush=True)
        why = static_arm(ROOT / "build/rust" / program)
        if why:
            fail(f"build/rust/{program}: {why}")
        size = (ROOT / "build/rust" / program).stat().st_size
        print(f"PASS build/rust/{program}: static ARM, EABI 5 hard-float, {size} bytes", flush=True)
    tests = unit_tests()
    steps = [f"echo '== unit tests: {test.name}' && {QEMU} {test} --test-threads=1 -q" for test in tests]
    for suite, programs in SUITES.items():
        builds = "rust=build/rust" if no_c & set(programs) else BUILDS
        steps.append(f"echo '== {suite}' && ENA_WORKERS={builds} python3 {suite}.py")
    command = ["docker", "run", "--rm", "-v", f"{ROOT}:/work:ro",
               "-v", f"{os.environ.get('HU_ROOTFS', 'entrynav2-rootfs')}:/sysroot:ro",
               "-e", "PYTHONDONTWRITEBYTECODE=1",
               *(["-e", "ENA_REFERENCE=fixtures"] if args else []),
               "-e", f"ENA_WORKER_RUNNER={QEMU} -L /sysroot", "-w", "/work/tests",
               "aa-entrynav-qemu:local", "sh", "-ec", " && ".join(steps)]
    print("+", shlex.join(command[:3]), "... aa-entrynav-qemu:local", flush=True)
    result = subprocess.run(command, env=dict(os.environ, MSYS_NO_PATHCONV="1"), check=False)
    if result.returncode:
        fail(f"the checks under QEMU (exit {result.returncode})")
    print("PASS every Rust check")


if __name__ == "__main__":
    main(sys.argv[1:])
