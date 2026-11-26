r"""What the equivalence tests (tests/test_worker_equivalence.py, tests/test_gauge_equivalence.py) compare each build
with: the C build, run live while it exists, or what the C build did, recorded in tests/fixtures/<worker>.json by
tests/record_fixtures.py. The fixtures keep the Rust checked against the C's behaviour once the C is gone.

ENA_REFERENCE chooses:
    live       the first build in ENA_WORKERS (the C), run through each scenario. Where a fixture has the scenario,
               the live C must also still match it, so a fixture that no longer says what the C does is noticed.
    fixtures   the recorded results. Every build that has the worker is compared with them, the C included.
    (unset)    live when the first build has the worker, the fixtures when it doesn't.

    ENA_REFERENCE=fixtures ENA_WORKERS=c=build-arm,rust=build/rust python3 tests/test_worker_equivalence.py
"""

import json
import os
import re
from pathlib import Path

from .workers import builds

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
# Ports picked at random for a run (unused_port), which the workers' lines may print: written as PORT, so that a result
# recorded once still compares.
PORTS = set()


class Recorded:
    """A recorded measurement: what a scenario's Within held, its value. Within.agrees(recorded) reads only that."""

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "None" if self.value is None else f"{self.value:.3f}"


def _encode(value):
    if hasattr(value, "agrees"):  # a Within, or a Recorded read back
        return {"within": value.value}
    raise TypeError(f"{type(value).__name__} isn't recordable")


def _decode(item):
    return Recorded(item["within"]) if set(item) == {"within"} else item


def _ports_out(text):
    for port in sorted(PORTS, key=lambda p: -len(str(p))):
        text = re.sub(rf"\b{port}\b", "PORT", text)
    return text


def plain(result):
    """result as JSON has it (tuples as lists, measurements as {"within": value}), with the random ports in stdout and
    stderr written as PORT: what is recorded, and what is compared."""
    data = json.loads(json.dumps(result, default=_encode))
    for key in ("stdout", "stderr"):
        if isinstance(data.get(key), str):
            data[key] = _ports_out(data[key])
    return data


def revived(data):
    """A recorded result with its measurements back as objects that Within.agrees can read."""
    return json.loads(json.dumps(data), object_hook=_decode)


def mode(worker):
    chosen = os.environ.get("ENA_REFERENCE", "")
    if chosen not in ("", "live", "fixtures"):
        raise SystemExit(f"ENA_REFERENCE: {chosen!r} is neither live nor fixtures")
    if chosen:
        return chosen
    _, folder = builds()[0]
    return "live" if (folder / worker).is_file() else "fixtures"


def fixture_path(worker):
    return FIXTURES / f"{worker}.json"


_loaded = {}


def recorded(worker, name):
    """The C's recorded result for scenario name, or None when it has none."""
    if worker not in _loaded:
        path = fixture_path(worker)
        _loaded[worker] = json.loads(path.read_text())["scenarios"] if path.is_file() else {}
    data = _loaded[worker].get(name)
    return None if data is None else revived(data)


def present(worker):
    """[(label, folder)] of the builds that have worker."""
    return [(label, folder) for label, folder in builds() if (folder / worker).is_file()]
