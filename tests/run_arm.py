#!/usr/bin/env python3
"""The ARM checks, with the car's own libraries under QEMU. Run in the QEMU image after the ARM build:

    bash toolchain/tc.sh make arm
    bash toolchain/qemu.sh python3 tests/run_arm.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSROOT = "/sysroot"
QEMU = ["qemu-arm", "-L", SYSROOT, "-E", "LD_LIBRARY_PATH=/opt/hmi/lib:/usr/local/lib:/usr/lib:/lib", "-E",
        "LD_BIND_NOW=1"]
ARM = ROOT / "build-arm"
PLUGINS = [("libBoostDeck.so", "BoostDeck", "_ZTV9BoostDeck"), ("libShiftDeck.so", "ShiftDeck", "_ZTV9ShiftDeck"),
           ("libMhdMaps.so", "MhdMaps", "_ZTV7MhdMaps")]


def run(name, command, expect="PASS"):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    if result.returncode != 0 or expect not in out:
        raise SystemExit(f"FAIL {name} (exit {result.returncode})\n{out}")
    print(out.strip() if expect == "PASS" else f"PASS {name}", flush=True)


def main():
    for lib, name, _ in PLUGINS:
        run(f"load {name}", [*QEMU, str(ARM / "test-plugin-load"), str(ARM / lib), name])
    run("vtables", [sys.executable, str(ROOT / "tests/check_vtable.py"), SYSROOT,
                    *[f"{ARM / lib}:{vtable}" for lib, _, vtable in PLUGINS]])
    run("worker spawn", [*QEMU, str(ARM / "test-spawn"), "--hu-glibc"])
    # The workers start and parse their arguments with the car's C library (demo mode sends to a local UDP port).
    run("boostdeck-collector demo", [*QEMU, str(ARM / "boostdeck-collector"), "--demo", "--cycles", "2"], "")
    print("PASS all ARM checks")


if __name__ == "__main__":
    main()
