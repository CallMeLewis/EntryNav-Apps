#!/usr/bin/env python3
"""Runs tests/test_setup.py: setup.sh on a pretend EntryNav2 made from the copy of the car's system.

    python3 tools/package_release.py && python3 tests/run_setup.py [TestCase.test_name ...]

The tests run in the disposable aa-entrynav-qemu:local container, started with --privileged so that they can mount
and chroot, with the copy of the car's system from the Docker volume entrynav2-rootfs (HU_ROOTFS to use another).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    command = ["docker", "run", "--rm", "--privileged",
               "-v", f"{os.environ.get('HU_ROOTFS', 'entrynav2-rootfs')}:/sysroot:ro",
               "-v", f"{ROOT}:/work:ro", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "aa-entrynav-qemu:local", "python3", "/work/tests/test_setup.py", *args]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
