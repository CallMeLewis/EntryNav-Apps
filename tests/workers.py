r"""Which builds of a worker the worker tests run, so that one test runs against the C and the Rust build alike.

ENA_WORKERS lists the builds as label=folder pairs separated by commas, folders relative to the repository. Without
it the tests run the PC build of the C workers, build/, as make test always has. ENA_WORKER_RUNNER is a command put
before every worker, such as qemu-arm for the ARM builds. tests/run_rust.py sets both:

    ENA_WORKERS=c=build-arm,rust=build/rust ENA_WORKER_RUNNER="qemu-arm -L /sysroot ..." \
        python3 tests/test_engine_codes.py
"""

import os
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def builds():
    """[(label, folder)], in the order ENA_WORKERS gives them."""
    pairs = []
    for item in os.environ.get("ENA_WORKERS", "c=build").split(","):
        label, _, folder = item.strip().partition("=")
        if not label or not folder:
            raise SystemExit(f"ENA_WORKERS: {item!r} is not label=folder")
        pairs.append((label, ROOT / folder))
    return pairs


def command(folder: Path, worker: str):
    """The command line that starts worker from folder, after ENA_WORKER_RUNNER."""
    return [*shlex.split(os.environ.get("ENA_WORKER_RUNNER", "")), str(folder / worker)]


def for_each_build(base, worker: str, namespace: dict):
    """Adds a copy of the test case base to namespace for every build, as <base name>_<label>, with self.worker set to
    that build's command line. base's own name is left out, so unittest runs only the copies."""
    for label, folder in builds():
        name = f"{base.__name__}_{label}"
        namespace[name] = type(name, (base, unittest.TestCase), {"worker": command(folder, worker), "build": label,
                                                                 "__module__": namespace["__name__"]})
