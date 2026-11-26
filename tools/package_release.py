#!/usr/bin/env python3
"""Build one release archive per plugin, which Flare Drive and setup.sh install on their own:
dist/<id>-<version>-armhf.tar for boostdeck, shiftdeck, bm3-maps, mhd-maps and engine-codes.

Build the ARM programs and the screens first (bash toolchain/tc.sh make arm, make assets). Each archive holds one
folder, <id>-<version>/, laid out like the head unit:

    setup.sh VERSION plugin.json MD5SUMS frameworks.md5
    opt/hmi/data/ro/Plugins/<App>/        the app, its screens, images and worker, ena-gateway, and setup.sh,
                                          VERSION, plugin.json
    etc/firewall/entrynav-<id>.rules

setup.sh and the rule file are install/setup.sh and install/firewall.rules with the plugin's details filled in. MD5SUMS
covers every other file (setup.sh check verifies it on the car, which has no sha256sum). The archives are
reproducible: fixed owner, modes and dates, sorted entries, USTAR as Flare Drive's contract asks.

    python3 tools/package_release.py [--workers c|rust] [OUT] [ID ...]

--workers chooses the worker and ena-gateway each archive holds: c, the default, takes build-arm's (bash toolchain/tc.sh
make arm); rust takes build/rust's static ports (make rust), which must pass tests/run_rust.py first. Without --workers,
ENA_RELEASE_WORKERS chooses, so tools/check.py and tools/publish_release.py follow it too. The app's library comes from
build-arm either way. Give rust archives their own OUT, such as dist/rust, so the two never mix.
"""

import argparse
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARM = ROOT / "build-arm"
RUST = ROOT / "build" / "rust"
PLUGIN = ROOT / "plugin"
VERSION = re.compile(r"^\d+\.\d+\.\d+(-(beta|test)\.\d+)?$")
ELF_ARM = b"\x7fELF\x01\x01\x01"
WORKER_BUILDS = ("c", "rust")


class Plugin:
    """former_name: the name before a rename. setup.sh still recognises the firewall rule an install under that name
    left, by its first line. The id, folder and worker keep their old names, so installs and feeds carry on."""

    def __init__(self, id, app, name, worker, settings="", former_name=""):
        self.id, self.app, self.name, self.worker, self.settings = id, app, name, worker, settings
        self.former_name = former_name


PLUGINS = [
    Plugin("boostdeck", "BoostDeck", "BoostDeck", "boostdeck-collector", "boostdeck-temperature"),
    Plugin("shiftdeck", "ShiftDeck", "ShiftDeck", "shift-deck", "shiftdeck-speed"),
    Plugin("bm3-maps", "Bm3Maps", "Bootmod3 Suite", "bm3-maps", "bm3-marker-address", former_name="BM3 Maps"),
    Plugin("mhd-maps", "MhdMaps", "MHD+ Suite", "mhd-maps"),
    Plugin("engine-codes", "EngineCodes", "Engine Fault Codes", "engine-codes"),
]


def fail(message):
    raise SystemExit(f"package_release: {message}")


def read(path: Path) -> bytes:
    if not path.is_file():
        fail(f"{path.relative_to(ROOT)} is missing: build it first")
    return path.read_bytes()


def arm(path: Path) -> bytes:
    data = read(path)
    if not data.startswith(ELF_ARM) or int.from_bytes(data[18:20], "little") != 40:  # EM_ARM
        fail(f"{path.relative_to(ROOT)} is not a 32-bit ARM ELF")
    return data


def static_arm(path: Path) -> bytes:
    """A Rust worker: a 32-bit ARM EABI 5 hard-float program with no interpreter or dynamic section, which needs
    nothing from the car's loader or C library (setup.sh check runs it instead of tracing it)."""
    data = arm(path)
    flags, = struct.unpack_from("<I", data, 36)
    if flags >> 24 != 5 or not flags & 0x400:
        fail(f"{path.relative_to(ROOT)} is not EABI 5 hard-float")
    phoff, = struct.unpack_from("<I", data, 28)
    size, count = struct.unpack_from("<HH", data, 42)
    if any(struct.unpack_from("<I", data, phoff + i * size)[0] in (2, 3) for i in range(count)):  # PT_DYNAMIC, PT_INTERP
        fail(f"{path.relative_to(ROOT)} is dynamically linked, not a static Rust worker")
    return data


def program(name: str, workers: str) -> bytes:
    """A worker or ena-gateway from the build --workers chose."""
    return static_arm(RUST / name) if workers == "rust" else arm(ARM / name)


def unix_text(path: Path) -> bytes:
    data = read(path)
    if b"\r\n" in data:
        fail(f"{path.relative_to(ROOT)} has Windows line endings")
    return data


def filled(path: Path, plugin: Plugin) -> bytes:
    """A template with the plugin's details in place of @ID@, @APP@, @NAME@, @FORMER_NAME@, @WORKER@ and
    @SETTINGS@."""
    text = unix_text(path).decode()
    for key, value in (("ID", plugin.id), ("APP", plugin.app), ("NAME", plugin.name),
                       ("FORMER_NAME", plugin.former_name), ("WORKER", plugin.worker), ("SETTINGS", plugin.settings)):
        text = text.replace(f"@{key}@", value)
    left = re.search(r"@[A-Z_]+@", text)
    if left:
        fail(f"{path.relative_to(ROOT)}: {left.group(0)} has no value")
    return text.encode()


def release_files(plugin: Plugin, version, workers="c"):
    """archive path inside the top folder -> (bytes, mode)."""
    files = {}
    description = {
        "schema": 1,
        "id": plugin.id,
        "name": plugin.name,
        "version": version,
        # Empty: the apps install on any firmware. Flare Drive 2.2.7 and older refuse firmware a non-empty list leaves out.
        "firmware": [],
        "installed_in": f"/opt/hmi/data/ro/Plugins/{plugin.app}",
    }
    meta = {
        "setup.sh": (filled(ROOT / "install/setup.sh", plugin), 0o755),
        "VERSION": (f"{version}\n".encode(), 0o644),
        "plugin.json": ((json.dumps(description, indent=2) + "\n").encode(), 0o644),
    }
    files.update(meta)
    files["frameworks.md5"] = (unix_text(ROOT / "install/frameworks.md5"), 0o644)
    base = f"opt/hmi/data/ro/Plugins/{plugin.app}"
    for name, entry in meta.items():
        files[f"{base}/{name}"] = entry
    files[f"{base}/lib{plugin.app}.so"] = (arm(ARM / f"lib{plugin.app}.so"), 0o644)
    files[f"{base}/{plugin.worker}"] = (program(plugin.worker, workers), 0o755)
    files[f"{base}/ena-gateway"] = (program("ena-gateway", workers), 0o755)
    for suffix in (".manifest", ".xml"):
        files[f"{base}/{plugin.app}{suffix}"] = (unix_text(PLUGIN / f"{plugin.app}{suffix}"), 0o644)
    files[f"{base}/{plugin.app}_ALL_Images.zip"] = (read(PLUGIN / f"{plugin.app}_ALL_Images.zip"), 0o644)
    files[f"etc/firewall/entrynav-{plugin.id}.rules"] = (filled(ROOT / "install/firewall.rules", plugin), 0o644)
    sums = "".join(f"{hashlib.md5(data).hexdigest()}  {name}\n" for name, (data, _) in sorted(files.items()))
    files["MD5SUMS"] = (sums.encode(), 0o644)
    return files


def source_epoch():
    try:
        return int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True, text=True,
                                  check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def build(plugin: Plugin, version, out: Path, epoch, workers="c") -> Path:
    files = release_files(plugin, version, workers)
    top = f"{plugin.id}-{version}"
    folders = set()
    for name in files:
        parts = name.split("/")[:-1]
        folders.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in [""] + sorted(folders):
            item = tarfile.TarInfo(f"{top}/{name}".rstrip("/"))
            item.type, item.mode, item.mtime = tarfile.DIRTYPE, 0o755, epoch
            tar.addfile(item)
        for name, (data, mode) in sorted(files.items()):
            item = tarfile.TarInfo(f"{top}/{name}")
            item.size, item.mode, item.mtime = len(data), mode, epoch
            tar.addfile(item, io.BytesIO(data))
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{top}-armhf.tar"
    archive.write_bytes(payload.getvalue())
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", choices=WORKER_BUILDS, default=os.environ.get("ENA_RELEASE_WORKERS") or "c",
                        help="the build of the workers and ena-gateway: c (build-arm) or rust (build/rust)")
    parser.add_argument("out", nargs="?", type=Path, default=ROOT / "dist")
    parser.add_argument("ids", nargs="*")
    options = parser.parse_args(argv)
    if options.workers not in WORKER_BUILDS:
        fail(f"ENA_RELEASE_WORKERS {options.workers!r} is neither c nor rust")
    wanted = set(options.ids)
    unknown = wanted - {p.id for p in PLUGINS}
    if unknown:
        fail(f"no plugin called {', '.join(sorted(unknown))}")
    version = (ROOT / "VERSION").read_text().strip()
    if not VERSION.match(version):
        fail(f"VERSION {version!r} is not X.Y.Z, X.Y.Z-beta.N or X.Y.Z-test.N")
    epoch = source_epoch()
    print(f"Workers: {'the Rust ports, from build/rust' if options.workers == 'rust' else 'the C ones, from build-arm'}")
    for plugin in PLUGINS:
        if wanted and plugin.id not in wanted:
            continue
        archive = build(plugin, version, options.out, epoch, options.workers)
        data = archive.read_bytes()
        print(f"{archive}  {len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
