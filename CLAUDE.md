# Notes for coding agents

## Rust

New components are written in Rust, in `rust/`; C and C++ get fixes only. The C workers are ported one at a time and
kept until the Rust one has matched them in the car (`tests/run_rust.py`, then `tools/try_on_car.py`). The plugin code
in `src/*.cpp` and `sdk/` stays C++: it has to match iDrive's C++ interface, built with GCC 4.7. Rust for the head unit
is static (`armv7-unknown-linux-musleabihf`) and never needs the car's own libraries changed.

Once a Rust worker ships in place of its C one (`tools/package_release.py --workers rust`) and that release has run in
the car, remove what it replaced in the same change: its code in `src/collector.c`, headers only it used, its Makefile
rules and its C-only tests. Before that, record the C worker's outputs as fixtures (`tests/record_fixtures.py`, into
`tests/fixtures`) so the Rust stays checked against the old behaviour; the equivalence tests use them once the C build
is gone. Delete files nothing references whenever you find them, after checking Flare Drive and AA-EntryNav don't use
them.

## Publishing

`tools/publish_release.py publish` sends each app's release to Flare Drive's plugin catalog through `flare-publish`
(Flare Drive's `tools/flare_publish.py`, in the repo beside this one). The publishing Worker checks and stores it, and
the per-plugin list for Flare Drive 2.2.1 and earlier is rebuilt from the catalog. `stability` changes the catalog the
same way. Read [Flare Drive's docs/publishing.md](../flare-drive/docs/publishing.md)
(https://github.com/CallMeLewis/flare-drive/blob/main/docs/publishing.md) before publishing or changing a release: it
describes the whole process.

- Never upload an archive or `index.json` to the bucket any other way, for example with Wrangler. Flare Drive 2.2.2 and
  later read only the catalog, so they would never see it.
- `retire` is refused: the catalog would keep listing the release. Withdraw the app or change a release on the
  publishing page (`publish.darkzone.dev`) instead, as that guide describes.
- `plugins/<id>.toml` is each app's manifest. Its `folder` and `worker` can never change, since installs on cars use
  them. Its name, summary, description and order only describe an app new to the catalog; edit them on the publishing
  page. A new app needs a manifest here and its id in `PLUGINS` in `tools/publish_release.py`.
