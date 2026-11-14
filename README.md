# EntryNav-Apps

Gauge apps for BMWs with the **EntryNav2** head unit, the one with a 1280×480 screen and CarPlay over Wi-Fi. They
appear under **My Vehicle** in iDrive and read the engine and gearbox through the car's diagnostic gateway.

- **BoostDeck**: boost, air-fuel ratio, ignition timing, intake air, gearbox, rail pressure, oil pressure and coolant.
- **ShiftDeck**: a full-width RPM bar with a shift light, and road speed.
- **MHD Maps**: map switching for MHD+ multi-map tunes. It is being replaced by a bootmod3 version.

> [!WARNING]
> Work in progress. The apps build and pass their checks against a copy of the EntryNav2's system, but they have not
> run in a car yet. Installing them changes the head unit's system. Do it parked, at your own risk.

This is a port of [inotbarnacle/ID6_apps_prerelease](https://github.com/inotbarnacle/ID6_apps_prerelease), which runs
these apps on the NBT Evo. The EntryNav2 has the same iDrive app framework, built for Linux rather than QNX. This fork
supports the EntryNav2 only.

<table>
  <tr>
    <td><img src="images/01_boostdeck_daylight.jpg" width="300"></td>
    <td><img src="images/05_shiftdeck.jpg" width="300"></td>
  </tr>
</table>

*The original apps on an NBT Evo, from the upstream project.*

## What the EntryNav2 needs that the NBT Evo didn't

| | NBT Evo (upstream) | EntryNav2 (this fork) |
|---|---|---|
| System | QNX | ARMv7 Linux, glibc 2.15 |
| App folder | `/var/opt/hmi/data/ID5/Plugins` | `/opt/hmi/data/ro/Plugins` on the system partition. `/var/local` doesn't allow programs to run |
| Settings | beside the apps | `/var/local/data/EntryNavApps` |
| `bmw::PluginApp` | 352 bytes | larger (a Linux semaphore), so the apps reserve 0x200 bytes |
| Gateway access | open | the head unit's firewall drops outgoing connections, so port 6801 needs a rule |
| Installer | desktop app over ENET | `setup.sh` on the head unit, or Flare Drive |

## Signals

BoostDeck's BMW-specific readings come from a 2016 340i with the B58 engine. Other engines may answer fewer of them or
scale them differently. Compare each reading with a logger you trust before relying on it.
[Signal table](docs/SIGNALS.md) · [Porting to another car](docs/PORTING.md)

ShiftDeck uses standard OBD engine speed and road speed, which nearly every car answers.

## Build

The ARM build uses the same Docker images as AA-EntryNav: `entrynav2-tc:4.7` to compile and `aa-entrynav-qemu:local`
to run the checks. Both need a copy of the car's system, a Docker volume named `entrynav2-rootfs` by default (set
`HU_ROOTFS` to use another). The copy never goes into git.

```sh
make test                                          # host tests: decoders and workers against a simulated gateway
make assets                                        # screens and images (needs Pillow)
bash toolchain/tc.sh make arm                      # ARM plugins, workers and test programs
bash toolchain/qemu.sh python3 tests/run_arm.py    # ARM checks with the car's libraries
```

`tests/run_arm.py` loads each plugin the way iDrive does, with the car's own HMI libraries under QEMU. It compares each
plugin's 34 virtual functions with the car's `bmw::PluginApp`, slot by slot. It also starts a worker with the car's C
library and the signal settings iDrive's process needs.

## Licence

MIT, like the upstream project. See [LICENSE](LICENSE) and [third-party](third-party/).
