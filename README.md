<div align="center">
  <h1>EntryNav-Apps</h1>
  <p>Engine gauges, a shift light, bootmod3 and MHD controls, and fault codes for BMW iDrive 6.</p>
  <p>
    <a href="https://github.com/CallMeLewis/EntryNav-Apps/releases"><img src="https://img.shields.io/github/v/release/CallMeLewis/EntryNav-Apps?display_name=tag&amp;sort=semver" alt="Latest release"></a>
    <a href="https://github.com/CallMeLewis/EntryNav-Apps/actions/workflows/ci.yml"><img src="https://github.com/CallMeLewis/EntryNav-Apps/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-blue" alt="Licence: MIT"></a>
  </p>
</div>

EntryNav-Apps adds five apps to BMWs with the **EntryNav2** head unit, the iDrive 6 unit with a 1280×480 screen and
CarPlay over Wi-Fi. They appear under *My Vehicle* in iDrive and read the engine through the car's diagnostic gateway.
Each app is its own plugin: install any of them, and remove one without touching the others.

| App | What it shows |
|---|---|
| [BoostDeck](#boostdeck) | Boost, air-fuel ratio, ignition timing, intake air, gearbox, rail pressure, oil pressure and coolant |
| [ShiftDeck](#shiftdeck) | A full-width rev bar with shift lights, and road speed |
| [Bootmod3 Suite](#bootmod3-suite) | bootmod3's four map slots, Active Sound and burbles, on a car running a bootmod3 CustomROM |
| [MHD+ Suite](#mhd-suite) | MHD's four maps and Max Cooling, on a car running an MHD+ multi-map tune |
| [Engine Fault Codes](#engine-fault-codes) | The engine computer's fault codes, with a description of each, and a way to clear them |

They're ported from [inotbarnacle's apps](https://github.com/inotbarnacle/ID6_apps_prerelease) for the NBT Evo. This
port runs on the EntryNav2 only: on an NBT Evo, use the originals.

> [!WARNING]
> EntryNav-Apps is experimental, and installing it changes the head unit's system. Install it with the car parked,
> entirely at your own risk. Bootmod3 Suite writes to the engine computer's memory, and MHD+ Suite sends commands to
> MHD's code in it. The authors accept no responsibility for any damage to your car or loss of its warranty.

## Installing

The easy way is **[Flare Drive](https://github.com/CallMeLewis/flare-drive-app)** 2.2.0 or later, the companion Android app. Each app has its own page under *Plugins*,
where Flare Drive downloads a signed release, checks that your car can run it, installs it and finds the diagnostic
gateway. Updates and removal happen there too. The [CHANGELOG](CHANGELOG.md) lists what changed in each release, and
[Releases](https://github.com/CallMeLewis/EntryNav-Apps/releases) has every archive with its SHA-256.

<details>
<summary><b>Installing by hand</b></summary>

You need an ENET cable, a PC with an SSH client, the head unit's root password and the app's archive. EntryNav-Apps
doesn't come with the password. Do it parked, with the ignition on. For BoostDeck:

1. Copy the archive to the head unit's `/tmp` and unpack it there: `tar -xf boostdeck-VERSION-armhf.tar -C /tmp`.
2. Run `sh /tmp/boostdeck-VERSION/setup.sh check`. It checks the car and writes nothing.
3. Run `sh /tmp/boostdeck-VERSION/setup.sh install`. Besides the app, it installs a firewall rule that lets it reach
   the diagnostic gateway, then finds the gateway and saves its address for every app.
4. Restart iDrive: hold the volume knob for about 30 seconds. The app appears under *My Vehicle*.

| App | Archive | Installed in |
|---|---|---|
| BoostDeck | `boostdeck-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/BoostDeck` |
| ShiftDeck | `shiftdeck-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/ShiftDeck` |
| Bootmod3 Suite | `bm3-maps-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/Bm3Maps` |
| MHD+ Suite | `mhd-maps-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/MhdMaps` |
| Engine Fault Codes | `engine-codes-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/EngineCodes` |

Once installed, each app's `setup.sh` sits in its folder, for example `/opt/hmi/data/ro/Plugins/BoostDeck/setup.sh`:

| Command | |
|---|---|
| `status` | Whether it's installed, whether its files are intact, and the gateway in use |
| `gateway [IP]` | Finds the gateway again, or checks and saves the one given. Every app uses the address |
| `tidy` | Deletes an earlier version iDrive was still using, after a restart |
| `uninstall [--purge]` | Removes the app. `--purge` also deletes its settings, and the gateway's address once no other app is left |

The apps install on any firmware, at your own risk. `check` warns when iDrive's app framework isn't the version the
apps were checked against (`install/frameworks.md5`), and still stops when a library or function the app needs is
missing on the car. It also stops when anything in the way wasn't installed by EntryNav-Apps, or when the system
partition is already writable. `status` reports when iDrive's app framework has changed since the install, as a
software update does.

</details>

## The apps

### BoostDeck

Eight engine readings in two iDrive-style lists, each with a bar along its row, updated about every 35 ms while the
page is open.

| Under OPTIONS | |
|---|---|
| *Gauge color* | The bars' colour: one of eight, or Multicolor for a different colour per reading |
| *Temperature* | Celsius or Fahrenheit |

The BMW-specific readings come from a 2016 340i with the B58 engine. Other engines may answer fewer of them or scale
them differently, so compare each reading with a logger you trust before relying on it. The
[signal table](docs/SIGNALS.md) lists every request, and [porting to another car](docs/PORTING.md) says how to check
a new one.

### ShiftDeck

A rev bar from 0 to 8,000 rpm with a row of shift lights, and road speed. It reads engine speed about every 20 ms, and
the bar eases between readings.

| Under OPTIONS | |
|---|---|
| *Shift light setting* | The shift point, from 5,800 to 6,700 rpm |
| *Speed* | MPH or km/h |

It uses the standard engine speed and road speed that nearly every car reports.

### Bootmod3 Suite

Switches between bootmod3's four map slots and marks the active one. Picking a tile writes that slot and reads it
back. Called BM3 Maps until 0.1.1.

| Under OPTIONS | |
|---|---|
| *Active Sound* | Off or On. Shows whether the engine computer accepted it |
| *Burbles* | Off, or aggression 4, 8 or 12. Written to the ROM's burble table and switch, and read back |

Map switching isn't a BMW function: bootmod3's code in the engine computer does it. So before anything else, the app
reads bootmod3's CustomROM marker from the engine computer's memory, and goes on only if it names one of the 150 ROMs
in [`src/bm3_profiles.h`](src/bm3_profiles.h). A stock engine computer, an MHD tune or an unlisted ROM gets a message,
and nothing is written. For MHD, use [MHD+ Suite](#mhd-suite).

- Pick only slots that hold a map for the fuel in the tank. The app can't tell what each slot contains.
- There's no exhaust flap setting: bootmod3 sets the flap in each map.
- Some engine computers refuse bootmod3's memory reads with "security access denied". Then the app can't work on that
  car.
- Where the marker sits in memory depends on the engine computer. The app tries each address bootmod3's own app does,
  and `/var/local/data/EntryNavApps/bm3-marker-address` (eight hex digits) sets one by hand.

### MHD+ Suite

Switches between MHD's four maps. Picking a tile asks the engine computer for that map, and the app counts the request
only when the engine computer echoes it back.

| Under OPTIONS | |
|---|---|
| *Max Cooling* | Off or On. On needs the engine running |

Map switching isn't a BMW function: MHD's code in the engine computer does it, so it needs an MHD+ multi-map tune. An
engine computer without one refuses the request, and the app says so.

- MHD has no known way to read the active map back. So no tile is marked, and the app says the engine computer accepted
  the map, not that the map is active.
- Pick only maps made for the fuel in the tank. The app can't tell what each map contains.
- The check engine light may flash after a switch. That's MHD showing the map change.
- There's no exhaust flap setting.
- The commands come from cauliflowermossgrape's Live Adjustments, which checked them on one MG1 engine computer. Other
  engine computers are untried.

### Engine Fault Codes

*Read engine codes* reads the engine computer's fault memory and shows four codes a page, each as Active, Pending or
Stored, with a description when one is known.

| Under OPTIONS | |
|---|---|
| *Next page* | The next four codes |
| *Clear engine codes...* | Asks first, then clears the fault memory and reads it again. Use it with the ignition on and the engine off |

The descriptions in [`src/dme_descriptions.h`](src/dme_descriptions.h) were collected on a 340i, so other engines show
some codes without one.

## How it works

Each app is a plugin that iDrive loads at start-up, with a worker program beside it that does the diagnostics, so
iDrive's own process never waits on the car. The workers reach the engine computer through the car's diagnostic
gateway on TCP 6801. They find the gateway themselves, on the link-local network or on the one that tethering on the
ENET cable hands out, and find it again when it moves.

- The apps live in `/opt/hmi/data/ro/Plugins` on the system partition, each with its own firewall rule in
  `/etc/firewall`, because the head unit's firewall drops outgoing connections.
- Their settings and the gateway's address live in `/var/local/data/EntryNavApps`, on the data partition.
- An app's folder only changes by a whole-folder rename, so a power cut leaves the previous version or the new one.

<details>
<summary><b>What the EntryNav2 needs that the NBT Evo didn't</b></summary>

| | NBT Evo (upstream) | EntryNav2 (this port) |
|---|---|---|
| System | QNX | ARMv7 Linux, glibc 2.15 |
| App folder | `/var/opt/hmi/data/ID5/Plugins` | `/opt/hmi/data/ro/Plugins` on the system partition. `/var/local` doesn't allow programs to run |
| Settings | beside the apps | `/var/local/data/EntryNavApps` |
| `bmw::PluginApp` | 352 bytes | larger (a Linux semaphore), so the apps reserve 0x200 bytes |
| Gateway access | open, at a fixed address | the head unit's firewall drops outgoing connections, so port 6801 needs a rule, and the apps search for the gateway |
| Installer | files copied over SSH | one archive per app with its own `setup.sh`, or Flare Drive |
| Tuning controls | MHD+ Suite or Bootmod3 Suite | both, each its own plugin |

</details>

## Building it yourself

You need Docker, and the same images as AA-EntryNav: `entrynav2-tc:4.7` to compile and `aa-entrynav-qemu:local` to run
the checks. Both use a copy of the car's system in a Docker volume, `entrynav2-rootfs` by default (`HU_ROOTFS` names
another). That copy holds BMW files and never goes into git.

| Command | What it does |
|---|---|
| `make test` | The PC tests: the decoders, and each worker against a simulated gateway (Linux or WSL) |
| `make assets` | The screens and their image archives (Python with Pillow) |
| `make check` | The PC tests, the apps' C++ compiled for errors, ShellCheck, and the committed screens matching their generators. CI runs each as its own job on every push |
| `bash toolchain/tc.sh make arm` | The ARM apps, workers and test programs |
| `bash toolchain/qemu.sh python3 tests/run_arm.py` | Loads each app the way iDrive does, with the car's own libraries under QEMU |
| `python3 tools/package_release.py` | One release archive per app, in `dist/`, with the C workers. `--workers rust` packages the Rust ones (see [The Rust workers](#the-rust-workers)) |
| `python3 tests/run_setup.py` | `setup.sh` on a pretend head unit built from the copy of the car's system |
| `python3 tools/check.py` | Every check above, in the order a release needs them |

CI can't hold the copy of the car's system, so the ARM build and its checks run only in `tools/check.py`, which
`tools/publish_release.py` runs before every release.

### The Rust workers

`rust/` holds Rust ports of all six workers beside their C originals in `src/collector.c`: `ena-gateway`,
`engine-codes`, BoostDeck's `boostdeck-collector`, ShiftDeck's `shift-deck`, Bootmod3 Suite's `bm3-maps` and MHD+
Suite's `mhd-maps`. They take the same arguments, send the same requests in the same order, print the same lines and
exit the same way. The releases ship the C workers unless you package the Rust ones, as the last part of this section
describes. `ena-capture`, built with them, records what a gauge worker sends, for the car trial below. `test-bm3` is
`tests/test_bm3.c` ported to Rust, and the bm3-maps tests run both programs.

| Command | What it does |
|---|---|
| `make rust` | The six workers, `ena-capture` and `test-bm3` for the head unit, statically linked, in `build/rust`, with the Rust version `rust/rust-toolchain.toml` pins. From WSL on Windows: `make rust CARGO=cargo.exe` |
| `python3 tests/run_rust.py` | Their unit tests, the worker tests on both builds, and two tests that run the C and Rust builds through the same scenarios and compare them, all under QEMU. Run `bash toolchain/tc.sh make arm` first. `--fixtures` compares the Rust builds with the recorded C instead |
| `python3 tests/record_fixtures.py` | Runs the C workers through every comparison scenario under QEMU and records what they did in `tests/fixtures` |
| `uv run --no-project --with paramiko tests/test_try_on_car.py` | Tests `tools/try_on_car.py` against a pretend head unit in Docker |

The one-shot workers are compared byte for byte: stdout, stderr, the exit code, every request the pretend gateway
received in order, files written, and how long a run takes when it waits out a timeout. `bm3-maps` writes to the engine
computer, so its scenarios cover every write: a map change accepted, refused with any code, unanswered, answered
wrongly or not taken, the same for the burbles and Active Sound, SIGTERM part way through a write, and each reason
nothing is written, such as an unknown ROM, a stock or MHD-tuned engine computer, or a held lease. The pretend engine
computer's memory after each run is compared too. `tools/make_bm3_profiles.py` writes the ROM table for both builds
from the same upstream file, and a unit test checks that the two tables match.

The gauge workers stream, so their comparison can't be byte for byte. It checks what can't depend on timing exactly,
such as each frame's format and the values each reading takes, and what does depend on it, such as the frame rate and
when the slow readings come, within a tolerance.

`tests/fixtures` holds what the C workers did in every scenario. Each comparison also checks the live C against that
recording. Once `build-arm` has no C workers, the comparisons check the Rust ones against the recording instead, so
the Rust stays checked after the C is deleted. Record the fixtures again only while the C exists, after adding or
changing a scenario, on a computer that isn't busy: the gauge recordings include timing.

#### Trying them in the car

`tools/try_on_car.py` runs the Rust workers on the head unit next to the installed C ones and compares the results.
It installs nothing and doesn't need Flare Drive. It uploads the workers to a new folder in `/tmp`, checks their
SHA-256 there, and deletes the folder when it finishes. Only the read-only jobs the apps give these workers run:
reading the engine's fault codes, checking and finding the gateway, streaming BoostDeck's and ShiftDeck's readings,
and reading the active map as Bootmod3 Suite does when its page opens. Every argument that writes to the engine
computer is refused: `engine-codes --clear`, `bm3-maps --map`, `--burbles` and `--sound`, and `mhd-maps --map` and
`--cooling`. Each worker that can search for the gateway also gets `--gateway-file` in that folder: if it finds the
gateway at a new address, it saves the address there and not in the apps' settings.

- `bm3-maps --status` identifies the ROM as the app does each time: it defines the dynamic DID F301 in the engine
  computer's RAM and reads the CustomROM marker through it, or straight from memory. Then it reads the active map. It
  writes no memory.
- Every job of `mhd-maps` asks MHD's code in the engine computer for something. So the script only checks how it
  answers a run without a map or Max Cooling, which sends nothing. Without MHD+ Suite installed there is no C
  `mhd-maps` to compare with, and the script says so and leaves it out.

You need the apps installed, a saved gateway (`setup.sh gateway`), and `build/rust` from `make rust`.

1. Park with the ignition on, and close every EntryNav-Apps page in iDrive. Start the engine for live engine readings.
   With it off, ShiftDeck's worker reads 0 rpm and BoostDeck's gets only what the engine computer gives at rest.
2. Join the laptop to the car's Wi-Fi, or plug it into the ENET cable. Don't share the laptop's internet connection
   over the cable, or the gateway takes an address from it.
3. Run the script with the head unit's address. Flare Drive's Connection page shows it, and over the cable it is
   usually `169.254.1.0`:

   ```
   uv run --no-project --with paramiko tools/try_on_car.py --host 169.254.1.0
   ```

   It asks for the root password, or takes it from `HEADUNIT_PASSWORD`. It never shows or saves the password.

For each job it prints both workers' exit code, run time and peak memory. For the one-shot workers, `engine-codes`,
`ena-gateway`, `bm3-maps` and `mhd-maps`, it also prints their output, which must be the same.

Two gauge workers can't hold the gateway at once, so the script runs the Rust one for 20 seconds, then the C one
(`--seconds` changes how long). Each sends its frames to `ena-capture` on a port of its own, so the apps' ports stay
untouched, and SIGTERM stops it, as the app does. Live readings change between the two runs, so the script compares
the two streams within these tolerances:

- Both sent readable frames, in order, stamped with the head unit's monotonic clock, and printed the same errors.
- Each reading appears in about the same share of frames, within 25 points.
- The frame rates are within 15% of each other.
- Each reading's median is within its tolerance: boost 3 psi, AFR 1.5, timing 8 degrees, the three temperatures 5 °C,
  rail pressure 40 bar, oil pressure 1 bar, engine speed 250 rpm and road speed 3 km/h.

The script prints each reading's median, the frame rate and the longest gap between frames for both. A good run ends
like this:

```
   Result: MATCH, the same readings, frame rates within 15% and medians within tolerance

6 of 6 compared runs matched.
Deleted /tmp/ena-rust.Ab3dE9 on the head unit
```

A fault code that changes state between the two runs makes the fault code lists differ, and revs that change a lot
between the two gauge runs make the medians differ. Run the script again. If the difference stays, the port has a bug,
and the output shows both versions.

The script stops before it uploads anything if an app's worker is running or holds the diagnostic lease, since the
two would take turns on the gateway. Close the page and run it again. During a gauge run it checks every second, and
if an app's worker has started, it stops its own worker and ends. Ctrl-C stops it at any time: it ends the processes
it started and deletes its folder. If the connection drops before that, the folder stays in `/tmp` until the head
unit restarts, and the script prints the command that deletes it.

`--worker` runs one worker, `engine-codes`, `ena-gateway`, `bm3-maps`, `mhd-maps`, `boostdeck-collector` or
`shift-deck`, and `--worker gauges` runs both gauge workers. `--worker-args` gives that one worker other read-only
arguments, such as `--worker-args "check --host 169.254.10.3"`, or `--worker-args "--host 169.254.10.3 --status
--dump"` to see each step of bootmod3's identification. The script exits 0 when every run matched, 1 when one
differed, 2 when it refused the arguments and 3 when a check stopped it.

#### Packaging the Rust workers

`python3 tools/package_release.py --workers rust dist/rust` builds the same archives with the workers and
`ena-gateway` from `build/rust` in place of the C ones. The app's library stays the same. It refuses a worker that
isn't a static ARM program. `ENA_RELEASE_WORKERS=rust` does the same for `tools/check.py` and
`tools/publish_release.py`, and `check.py` then runs `tests/run_rust.py` as well. Without either, the archives hold the
C workers. `python3 tests/run_setup.py --dist dist/rust` runs the `setup.sh` and Flare Drive tests on those archives.

A static program needs no library from the car, and the car's loader crashes when it traces one, so `setup.sh check`
doesn't trace a static worker. It runs it with no arguments instead, and the worker must print its usage and exit 2
within 5 seconds. The app's library must still pass the loader's trace.

### Publishing a release

Every app shares one version, `VERSION`, and every release ships all of them: a change to one app raises `VERSION`, and
the others ship again unchanged. `tools/publish_release.py publish` runs `tools/check.py`, then sends each app's archive
to Flare Drive's plugin catalog with `flare-publish`, and `catalog/<id>.toml` is each app's manifest. It then tags
`vX.Y.Z` and puts the archives on its GitHub Release, with the version's CHANGELOG section as its text. The publishing
Worker checks and stores the archive and signs the catalog, and the list older Flare Drive versions read
(`https://updates.darkzone.dev/<id>/`) is rebuilt from it. It needs Flare Drive's repo cloned beside this one (or `FLARE_DRIVE_REPO`), the publishing service
token and the release signing key, and refuses a dirty or unpushed tree and a version
whose CHANGELOG section lacks an app's Flare Drive note. `stability` changes a release in the catalog; `retire` is
refused. Flare Drive's `docs/publishing.md` describes the whole process.

A retry checks the catalog, publishes only missing apps, and downloads the existing archives with size and SHA-256
checks to complete the GitHub Release. This also works when all apps are already published. MHD+ Suite uses the
catalog alone; older Flare Drive versions have no per-plugin list for it.

## Credits and licence

EntryNav-Apps is a port of [ID6_apps_prerelease](https://github.com/inotbarnacle/ID6_apps_prerelease) by inotbarnacle.
Bootmod3 Suite's protocol and ROM profiles come from [cauliflowermossgrape](https://github.com/cauliflowermossgrape)'s
port of BM3 Live by ezl0l, under the MIT licence in [third-party](third-party/cauliflowermossgrape-LICENSE).
`tools/make_bm3_profiles.py` regenerates the ROM table for the C and the Rust worker from that port's `bm3_profiles.js`. MHD+ Suite's commands come
from the same author's Live Adjustments app, from a1-mapping's captures.

EntryNav-Apps is under the [MIT licence](LICENSE), like the project it comes from. It comes with no warranty.

EntryNav-Apps is an independent project. It is not made or endorsed by BMW, by bootmod3's makers or by MHD's. BMW and
iDrive are trademarks of BMW AG.
