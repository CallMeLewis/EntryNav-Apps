#!/usr/bin/env python3
"""Publish EntryNav-Apps releases for Flare Drive to download, one feed per plugin:
https://updates.darkzone.dev/boostdeck/, /shiftdeck/, /bm3-maps/, /mhd-maps/ and /engine-codes/

  uv run --no-project --with cryptography python tools/publish_release.py init [ID...]      an empty signed index, once
  uv run --no-project --with cryptography python tools/publish_release.py verify [ID...]    check the live indexes
  uv run --no-project --with cryptography python tools/publish_release.py publish [ID...] [--stable]
  uv run --no-project --with cryptography python tools/publish_release.py stability ID VERSION stable|experimental
  uv run --no-project --with cryptography python tools/publish_release.py retire ID VERSION...   refused, see below

Without IDs, a command covers boostdeck, shiftdeck, bm3-maps, mhd-maps and engine-codes.

Releases reach Flare Drive through its plugin catalog, which only the publishing Worker writes. `publish` sends each
plugin's archive there with flare-publish (Flare Drive's tools/flare_publish.py, in its repo beside this one, or
FLARE_DRIVE_REPO), with plugins/<id>.toml as its manifest. The Worker checks the archive, stores it, adds it to the
catalog and signs it; flare-publish then rebuilds that plugin's per-plugin list (<id>/index.json) from the catalog and
signs it with the release key (~/.flare-drive/release-signing-key, or FLARE_DRIVE_SIGNING_KEY), for Flare Drive 2.2.1
and earlier. Flare Drive's docs/publishing.md describes the whole process. Never upload to the bucket any other way:
Flare Drive 2.2.2 and later read only the catalog.

`publish` refuses a dirty tree, a HEAD that is not pushed, a version already published and a version whose CHANGELOG.md
section lacks a plugin's Flare Drive note (<!-- Flare Drive <id>: ... -->). It runs tools/check.py first, then sends
each plugin's archive. Each release is experimental unless --stable; `stability` changes that later in the catalog.
`retire` is refused: the catalog would keep listing the release, and the Worker can't take one off yet.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "darkzone-updates"
BASE_URL = "https://updates.darkzone.dev"
PLUGINS = ("boostdeck", "shiftdeck", "bm3-maps", "mhd-maps", "engine-codes")
# flare-publish, which sends releases to Flare Drive's plugin catalog (docs/publishing.md in Flare Drive).
FLARE_DRIVE = Path(os.environ.get("FLARE_DRIVE_REPO", ROOT.parent / "flare-drive"))
FLARE_PUBLISH = FLARE_DRIVE / "tools" / "flare_publish.py"
WRANGLER = "wrangler@4.146.0"  # the version the Flare apps pin
# The first Flare Drive version code that knows these plugins (2.1.1 is 31). An older app never reads these feeds.
MIN_APP = 32
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION = re.compile(r"^\d+\.\d+\.\d+(-(beta|test)\.\d+)?$")


def key_path() -> Path:
    return Path(os.environ.get("FLARE_DRIVE_SIGNING_KEY", Path.home() / ".flare-drive" / "release-signing-key"))


def load_key() -> Ed25519PrivateKey:
    path = key_path()
    if not path.is_file():
        sys.exit(f"FAIL: no signing key at {path}. It is AA-EntryNav's key: see its tools/publish_release.py keygen.")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text().strip()))


def run(*args):
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def output(*args) -> str:
    return subprocess.run([str(a) for a in args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def flare_publish(*args):
    """Runs flare-publish with this Python, which has cryptography for the per-plugin list's signature. Stops on any
    failure, which flare-publish has already explained."""
    if not FLARE_PUBLISH.is_file():
        sys.exit(f"FAIL: no flare-publish at {FLARE_PUBLISH}. Clone Flare Drive beside this repo, or set FLARE_DRIVE_REPO.")
    if subprocess.run([sys.executable, str(FLARE_PUBLISH), *args], cwd=FLARE_DRIVE).returncode:
        sys.exit(f"FAIL: flare-publish {args[0]} failed, above.")


def wrangler_object(plugin, action, key, *options):
    npx = shutil.which("npx")
    if not npx:
        sys.exit(f"FAIL: npx not found. Install Node.js, then sign in once with: npx {WRANGLER} login")
    run(npx, "--yes", WRANGLER, "r2", "object", action, f"{BUCKET}/{plugin}/{key}", "--remote", *options)


def wrangler_put(plugin, key, file, content_type, cache_control):
    wrangler_object(plugin, "put", key, "--file", file, "--content-type", content_type, "--cache-control", cache_control)


def fetch(plugin, name):
    """The live file, or None when it does not exist yet. Bypasses Cloudflare's cache."""
    url = f"{BASE_URL}/{plugin}/{name}?t={datetime.now().timestamp()}"
    request = urllib.request.Request(url, headers={"User-Agent": "FlareDrivePublish"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def live_index(key, plugin):
    """The published index after checking its signature, or None if none is published."""
    index, signature = fetch(plugin, "index.json"), fetch(plugin, "index.json.sig")
    if index is None:
        return None
    if signature is None:
        sys.exit(f"FAIL: {plugin}/index.json is published without index.json.sig. Fix the bucket by hand before publishing.")
    try:
        key.public_key().verify(base64.b64decode(signature), index)
    except InvalidSignature:
        sys.exit(f"FAIL: {plugin}'s published index is not signed by this key. Nothing was changed.")
    return json.loads(index)


def publish_index(key, plugin, index):
    data = (json.dumps(index, indent=2) + "\n").encode()
    signature = base64.b64encode(key.sign(data)).decode() + "\n"
    key.public_key().verify(base64.b64decode(signature), data)
    with tempfile.TemporaryDirectory() as folder:
        index_file, sig_file = Path(folder) / "index.json", Path(folder) / "index.json.sig"
        index_file.write_bytes(data)
        sig_file.write_text(signature)
        # The signature first: an app fetching between the two uploads rejects the pair and keeps its last index.
        wrangler_put(plugin, "index.json.sig", sig_file, "text/plain", "public, max-age=60")
        wrangler_put(plugin, "index.json", index_file, "application/json", "public, max-age=60")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flare_drive_note(version: str, plugin: str) -> str:
    if not CHANGELOG.is_file():
        sys.exit("FAIL: no CHANGELOG.md.")
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## \[|\Z)", text, re.M | re.S)
    if not match:
        sys.exit(f"FAIL: CHANGELOG.md has no section for {version}.")
    note = re.search(rf"<!--\s*Flare Drive {re.escape(plugin)}:(.*?)-->", match.group(0), re.S)
    if not note or not note.group(1).strip():
        sys.exit(f"FAIL: CHANGELOG.md's {version} section has no <!-- Flare Drive {plugin}: ... --> note.")
    return " ".join(note.group(1).split())


def chosen(ids):
    unknown = sorted(set(ids) - set(PLUGINS))
    if unknown:
        sys.exit(f"FAIL: no plugin called {', '.join(unknown)}. They are {', '.join(PLUGINS)}.")
    return [p for p in PLUGINS if not ids or p in ids]


def init(args):
    key = load_key()
    for plugin in chosen(args.ids):
        if live_index(key, plugin) is not None:
            print(f"{plugin}: an index is already published; left as it is.")
            continue
        publish_index(key, plugin, {"schema": 1, "published": now(), "releases": []})
    verify(args)


def verify(args):
    key = load_key()
    for plugin in chosen(args.ids):
        index = live_index(key, plugin)
        if index is None:
            print(f"{BASE_URL}/{plugin}/index.json: none published yet. Run init.")
            continue
        print(f"{BASE_URL}/{plugin}/index.json: signature OK, published {index['published']}")
        for release in index["releases"]:
            print(f"  {release['version']}  {release['sha256'][:12]}…  {release['size']} bytes  "
                  f"{release.get('stability') or 'no label'}  {release['published']}")
        if not index["releases"]:
            print("  (no releases yet)")


def publish(args):
    key = load_key()
    plugins = chosen(args.ids)
    indexes = {plugin: live_index(key, plugin) for plugin in plugins}
    for plugin, index in indexes.items():
        if index is None:
            sys.exit(f"FAIL: no index is published for {plugin} yet. Run init first.")
    if output("git", "status", "--porcelain"):
        sys.exit("FAIL: the working tree has changes. Publish only what is committed.")
    head = output("git", "rev-parse", "HEAD")
    if not output("git", "branch", "-r", "--contains", head):
        sys.exit("FAIL: HEAD is not pushed. Push it first.")
    version = (ROOT / "VERSION").read_text().strip()
    if not VERSION.match(version):
        sys.exit(f"FAIL: VERSION says {version}, which is not X.Y.Z, X.Y.Z-beta.N or X.Y.Z-test.N.")
    for plugin, index in indexes.items():
        if any(r["version"] == version for r in index["releases"]):
            sys.exit(f"FAIL: {plugin} {version} is already published. Raise VERSION; a version is never replaced.")
    if args.stable and "-" in version:
        sys.exit(f"FAIL: {version} is a beta or test build: it is always experimental.")
    notes = {plugin: flare_drive_note(version, plugin) for plugin in plugins}
    for plugin, note in notes.items():
        print(f"{plugin} note: {note}", flush=True)
    run(sys.executable, "tools/check.py")
    out = ROOT / "dist"
    shutil.rmtree(out, ignore_errors=True)
    run(sys.executable, "tools/package_release.py", out, *plugins)
    for plugin in plugins:
        archive = out / f"{plugin}-{version}-armhf.tar"
        data = archive.read_bytes()
        with tarfile.open(archive) as tar:
            recorded = tar.extractfile(f"{plugin}-{version}/VERSION").read().decode().strip()
            described = json.loads(tar.extractfile(f"{plugin}-{version}/plugin.json").read())
        if recorded != version or described["version"] != version or described["id"] != plugin:
            sys.exit(f"FAIL: the {plugin} archive says {described['id']} {recorded}, not {plugin} {version}.")
        entry = {
            "version": version,
            "file": f"releases/{version}/{plugin}-{version}-armhf.tar",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "sourceCommit": head,
            "published": now(),
            "firmware": described["firmware"],
            "stability": "stable" if args.stable else "experimental",
            "minApp": MIN_APP,
            "notes": notes[plugin],
        }
        # The Worker checks, stores and adds it to the catalog; flare-publish then rebuilds this plugin's list.
        with tempfile.TemporaryDirectory() as folder:
            notes_file = Path(folder) / "notes.txt"
            notes_file.write_text(entry["notes"], encoding="utf-8", newline="\n")
            flare_publish("release", str(archive), "--manifest", str(ROOT / "plugins" / f"{plugin}.toml"),
                          "--stability", entry["stability"], "--notes-file", str(notes_file),
                          "--source-commit", head, "--min-app", str(MIN_APP))
        if fetch(plugin, entry["file"]) != data:
            sys.exit(f"FAIL: the published {plugin} archive doesn't read back as {archive.name}. Check with flare-publish verify.")
        print(f"Published {plugin} {version} ({entry['sha256'][:12]}…) from {head[:7]}.", flush=True)
    verify(args)


def stability(args):
    key = load_key()
    index = live_index(key, chosen([args.id])[0])
    entries = [r for r in (index or {}).get("releases", []) if r["version"] == args.version]
    if not entries:
        sys.exit(f"FAIL: {args.id} {args.version} is not published.")
    if args.label == "stable" and "-" in args.version:
        sys.exit(f"FAIL: {args.version} is a beta or test build: it stays experimental.")
    # Through the catalog, then the per-plugin list rebuilt from it.
    flare_publish("edit-release", args.id, args.version, "--stability", args.label)
    args.ids = [args.id]
    verify(args)


def retire(args):
    """Refuses: releases come from Flare Drive's catalog, which would keep listing a release taken off this list, and
    its next rebuild of the list would put it back. Flare Drive's docs/publishing.md says what to do instead."""
    sys.exit(
        "FAIL: retire isn't available now releases come from Flare Drive's catalog, which would keep listing "
        f"{args.id} {', '.join(args.versions)}. See docs/publishing.md in Flare Drive. Nothing was changed."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, func in (("init", init), ("verify", verify)):
        p = commands.add_parser(name)
        p.add_argument("ids", nargs="*")
        p.set_defaults(func=func)
    p = commands.add_parser("publish")
    p.add_argument("ids", nargs="*")
    p.add_argument("--stable", action="store_true")
    p.set_defaults(func=publish)
    p = commands.add_parser("stability")
    p.add_argument("id")
    p.add_argument("version")
    p.add_argument("label", choices=["stable", "experimental"])
    p.set_defaults(func=stability)
    p = commands.add_parser("retire")
    p.add_argument("id")
    p.add_argument("versions", nargs="+")
    p.add_argument("--delete-archives", action="store_true")
    p.set_defaults(func=retire)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
