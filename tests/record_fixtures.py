#!/usr/bin/env python3
"""Records what the C workers do in every scenario of tests/test_worker_equivalence.py and
tests/test_gauge_equivalence.py, under QEMU, as tests/fixtures/<worker>.json. With these the equivalence tests keep
checking the Rust workers against the C's behaviour after the C is deleted (tests/support/reference.py). Record them
again only while the C still exists, after a scenario was added or changed, and commit them with the change.

    bash toolchain/tc.sh make arm
    python3 tests/record_fixtures.py [WORKER ...]

It runs in the disposable aa-entrynav-qemu:local container, as tests/run_rust.py does, with the copy of the car's
system from the Docker volume entrynav2-rootfs (HU_ROOTFS to use another), read-only. Only tests/fixtures is writable
there. Each file holds the C build's SHA-256, and per scenario what the equivalence test compares: the output, exit code
and requests exactly, and the measurements (run time, frame counts and gaps) that the test compares within its
tolerances.
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
QEMU = "qemu-arm -cpu cortex-a15 -r 3.12.25-FR21.0"  # as tests/run_rust.py runs the workers
C_BUILD = "build-arm"


def record(workers):
    """Inside the container: runs each scenario on the C build and writes the fixtures."""
    from support import reference
    import test_gauge_equivalence
    import test_worker_equivalence
    from support.workers import command

    scenarios = {**test_worker_equivalence.SCENARIOS, **test_gauge_equivalence.SCENARIOS}
    unknown = set(workers) - set(scenarios)
    if unknown:
        raise SystemExit(f"no scenarios for {', '.join(sorted(unknown))}; there are {', '.join(scenarios)}")
    for worker in workers or scenarios:
        program = ROOT / C_BUILD / worker
        if not program.is_file():
            raise SystemExit(f"{C_BUILD}/{worker} is missing: bash toolchain/tc.sh make arm")
        results, skipped = {}, []
        for name, scenario in scenarios[worker].items():
            try:
                results[name] = reference.plain(scenario(command(ROOT / C_BUILD, worker)))
            except unittest.SkipTest:
                skipped.append(name)
                continue
            print(f"  {worker}: {name}", flush=True)
        fixture = {"worker": worker,
                   "recorded from": f"{C_BUILD}/{worker}",
                   "sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                   "runner": os.environ.get("ENA_WORKER_RUNNER", ""),
                   "scenarios": results}
        path = reference.fixture_path(worker)
        path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"WROTE tests/fixtures/{path.name}: {len(results)} scenarios"
              + (f", {len(skipped)} skipped ({'; '.join(skipped)})" if skipped else ""), flush=True)


def main(args):
    if args[:1] == ["--inside"]:
        sys.path.insert(0, str(ROOT / "tests"))
        record(args[1:])
        return 0
    FIXTURES.mkdir(exist_ok=True)
    # --init: the recorder must not be the container's pid 1, since the gauge workers stop when their parent is 1.
    command = ["docker", "run", "--rm", "--init", "-v", f"{ROOT}:/work:ro", "-v", f"{FIXTURES}:/work/tests/fixtures",
               "-v", f"{os.environ.get('HU_ROOTFS', 'entrynav2-rootfs')}:/sysroot:ro",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", f"ENA_WORKERS=c={C_BUILD}", "-e", "ENA_REFERENCE=live",
               "-e", f"ENA_WORKER_RUNNER={QEMU} -L /sysroot", "-w", "/work/tests",
               "aa-entrynav-qemu:local", "python3", "record_fixtures.py", "--inside", *args]
    print("+", shlex.join(command[:3]), "... aa-entrynav-qemu:local", flush=True)
    return subprocess.run(command, env=dict(os.environ, MSYS_NO_PATHCONV="1"), check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
