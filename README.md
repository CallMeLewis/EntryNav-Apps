<div align="center">
  <h1>EntryNav-Apps</h1>
  <p>Engine gauges, a shift light, bootmod3 controls and fault codes for BMW iDrive 6.</p>
  <p>
    <a href="https://github.com/CallMeLewis/EntryNav-Apps/releases"><img src="https://img.shields.io/github/v/release/CallMeLewis/EntryNav-Apps?display_name=tag&amp;sort=semver" alt="Latest release"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-blue" alt="Licence: MIT"></a>
  </p>
</div>

EntryNav-Apps adds four apps to BMWs with the **EntryNav2** head unit, the iDrive 6 unit with a 1280×480 screen and
CarPlay over Wi-Fi. They appear under *My Vehicle* in iDrive and read the engine through the car's diagnostic gateway.
Each app is its own plugin: install any of them, and remove one without touching the others.

| App | What it shows |
|---|---|
| **BoostDeck** | Boost, air-fuel ratio, ignition timing, intake air, gearbox, rail pressure, oil pressure and coolant |
| **ShiftDeck** | A full-width rev bar with shift lights, and road speed |
| **Bootmod3 Suite** | bootmod3's four map slots, Active Sound and burbles, on a car running a bootmod3 CustomROM |
| **Engine Fault Codes** | The engine computer's fault codes, with a description of each, and a way to clear them |

They're ported from [inotbarnacle's apps](https://github.com/inotbarnacle/ID6_apps_prerelease) for the NBT Evo. This
port runs on the EntryNav2 only: on an NBT Evo, use the originals.

> [!WARNING]
> EntryNav-Apps is experimental, and installing it changes the head unit's system. Install it with the car parked,
> entirely at your own risk. Bootmod3 Suite writes to the engine computer's memory. The authors accept no
> responsibility for any damage to your car or loss of its warranty.

## Installing

The easy way is **Flare Drive** 2.2.0 or later, the companion Android app. Each app has its own page under *Plugins*,
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
| Engine Fault Codes | `engine-codes-VERSION-armhf.tar` | `/opt/hmi/data/ro/Plugins/EngineCodes` |

Once installed, each app's `setup.sh` sits in its folder, for example `/opt/hmi/data/ro/Plugins/BoostDeck/setup.sh`:

| Command | |
|---|---|
| `status` | Whether it's installed, whether its files are intact, and the gateway in use |
| `gateway [IP]` | Finds the gateway again, or checks and saves the one given. Every app uses the address |
| `tidy` | Deletes an earlier version iDrive was still using, after a restart |
| `uninstall [--purge]` | Removes the app. `--purge` also deletes its settings, and the gateway's address once no other app is left |

`check` stops unless iDrive's app framework is the exact version the apps were checked against
(`install/frameworks.md5`). It also stops when anything in the way wasn't installed by EntryNav-Apps, or when the
system partition is already writable. An app's folder only changes by a whole-folder rename, so a power cut leaves the
previous version or the new one.

</details>

## What it does

The apps find the car's diagnostic gateway themselves, on the link-local network or on the one that tethering on the
ENET cable hands out, and find it again when it moves. Settings live in `/var/local/data/EntryNavApps`.

### BoostDeck

Eight readings, each on its own card, about every 35 ms while the page is open. *Gauge color* under OPTIONS picks the
bars' colour, and *Temperature* picks Celsius or Fahrenheit.

The BMW-specific readings come from a 2016 340i with the B58 engine. Other engines may answer fewer of them or scale
them differently, so compare each reading with a logger you trust before relying on it. The
[signal table](docs/SIGNALS.md) lists every request, and [porting to another car](docs/PORTING.md) says how to check
a new one.

### ShiftDeck

A rev bar from 0 to 8,000 rpm with a row of shift lights, and road speed. It reads engine speed about every 20 ms,
and the bar eases between readings. *Shift light setting* under OPTIONS sets the shift point, from 5,800 to
6,700 rpm, and *Speed* picks MPH or km/h. It uses the standard engine speed and road speed that nearly every car
reports.

### Bootmod3 Suite

Called BM3 Maps until 0.1.1. Map switching isn't a BMW function: bootmod3's code in the engine computer does it.
Bootmod3 Suite reads bootmod3's CustomROM marker from the engine computer's memory and goes on only if the marker
names one of the 150 ROMs in [`src/bm3_profiles.h`](src/bm3_profiles.h). A stock engine computer, an MHD tune or an
unlisted ROM gets a message, and nothing is written.

The page shows four map tiles and marks the active one. Picking a tile writes that slot and reads it back. OPTIONS has
two more screens, which check the marker the same way first:

| Screen | Choices | What it does |
|---|---|---|
| *Active Sound* | Off, On | Sends the setting. Shows whether the engine computer accepted it |
| *Burbles* | Off, Aggression 4, 8, 12 | Writes the ROM's burble table and switch, and reads both back |

- Pick only slots that hold a map for the fuel in the tank. The app can't tell what each slot contains.
- There's no exhaust flap screen: bootmod3 sets the flap in each map.
- Some engine computers refuse bootmod3's memory reads with "security access denied". Then the app can't work on that
  car.
- Where the marker sits in memory depends on the engine computer. The app tries each address bootmod3's own app does,
  and `/var/local/data/EntryNavApps/bm3-marker-address` (eight hex digits) sets one by hand.

### Engine Fault Codes

*Read engine codes* reads the engine computer's fault memory and shows four codes a page, each as Active, Pending or
Stored, with a description when [`src/dme_descriptions.h`](src/dme_descriptions.h) has one. Those descriptions were
collected on a 340i, so other engines show some codes without one. OPTIONS has *Next page* and *Clear engine
codes...*, which asks first, clears the fault memory and reads it again. Clear with the ignition on and the engine off.

## Building it yourself

You need Docker, and the same images as AA-EntryNav: `entrynav2-tc:4.7` to compile and `aa-entrynav-qemu:local` to run
the checks. Both use a copy of the car's system in a Docker volume, `entrynav2-rootfs` by default (`HU_ROOTFS` names
another). That copy holds BMW files and never goes into git.

| Command | What it does |
|---|---|
| `make test` | The PC tests: the decoders, and each worker against a simulated gateway (Linux or WSL) |
| `make assets` | The screens and their image archives (Python with Pillow) |
| `bash toolchain/tc.sh make arm` | The ARM apps, workers and test programs |
| `bash toolchain/qemu.sh python3 tests/run_arm.py` | Loads each app the way iDrive does, with the car's own libraries under QEMU |
| `python3 tools/package_release.py` | One release archive per app, in `dist/` |
| `python3 tests/run_setup.py` | `setup.sh` on a pretend head unit built from the copy of the car's system |
| `python3 tools/check.py` | Every check above, in the order a release needs them |

`tools/publish_release.py publish` runs `tools/check.py`, then uploads each archive and its signed index to the feeds
Flare Drive reads, at `https://updates.darkzone.dev/<id>/`. It needs the release signing key and a Wrangler login, and
refuses a dirty or unpushed tree, a version already published, and a version whose CHANGELOG section lacks an app's
Flare Drive note.

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
| Tuning controls | MHD+ | bootmod3 CustomROM |

</details>

## Credits and licence

EntryNav-Apps is a port of [ID6_apps_prerelease](https://github.com/inotbarnacle/ID6_apps_prerelease) by inotbarnacle.
Bootmod3 Suite's protocol and ROM profiles come from [cauliflowermossgrape](https://github.com/cauliflowermossgrape)'s
port of BM3 Live by ezl0l, under the MIT licence in [third-party](third-party/cauliflowermossgrape-LICENSE).
`tools/make_bm3_profiles.py` regenerates the ROM table from that port's `bm3_profiles.js`.

EntryNav-Apps is under the [MIT licence](LICENSE), like the project it comes from. It comes with no warranty.

EntryNav-Apps is an independent project. It is not made or endorsed by BMW or by bootmod3's makers. BMW and iDrive are
trademarks of BMW AG.
