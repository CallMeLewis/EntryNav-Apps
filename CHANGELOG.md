# Changelog

Each release's section holds one Flare Drive note per plugin, the paragraph the app shows with that plugin's release,
as `<!-- Flare Drive boostdeck: ... -->`, `<!-- Flare Drive shiftdeck: ... -->` and `<!-- Flare Drive bm3-maps: ... -->`.
`tools/publish_release.py publish` refuses a version without the notes of the plugins it publishes.

## [0.1.0-test.3]

<!-- Flare Drive bm3-maps: BM3 Maps now recognises bootmod3 on F-series engine computers, as on the M140i's B58, and shows the active map instead of "The DME refused to read its memory (NRC 72)". It looks where bootmod3's own app looks. -->

- **BM3 Maps finds the bootmod3 marker on F-series engine computers.** It tries the addresses bootmod3's own app
  tries, in its order, and reads the F-series marker, which starts with `#DST` and names the stock ROM before the
  bootmod3 one. Upstream knew only the MG1 address and layout, so an F20 M140i got NRC 72. `--dump` shows each step
  and the marker's bytes, for the next car that doesn't match.

## [0.1.0-test.2]

<!-- Flare Drive boostdeck: Find the diagnostic gateway now finds it after your phone's Ethernet tethering or a laptop has been on the ENET cable, when the gateway has an address such as 10.x.y.54 instead of 169.254.x.y. Flare Drive 2.1.7 or later shows that address. -->
<!-- Flare Drive shiftdeck: Find the diagnostic gateway now finds it after your phone's Ethernet tethering or a laptop has been on the ENET cable, when the gateway has an address such as 10.x.y.54 instead of 169.254.x.y. Flare Drive 2.1.7 or later shows that address. -->
<!-- Flare Drive bm3-maps: Find the diagnostic gateway now finds it after your phone's Ethernet tethering or a laptop has been on the ENET cable, when the gateway has an address such as 10.x.y.54 instead of 169.254.x.y. Flare Drive 2.1.7 or later shows that address. -->

- **Finding the gateway works after a phone or laptop has been on the ENET cable.** With Ethernet tethering or a
  laptop's DHCP server on the cable, the gateway takes an address from it, such as 10.x.y.54, and keeps it for a while
  after the cable comes out. The search only asked the 169.254 network and never found it there. It now asks every
  network on the head unit's car-side Ethernet, and the apps and the firewall rule accept the address it saves.

## [0.1.0-test.1]

<!-- Flare Drive boostdeck: The first test build. BoostDeck shows live engine readings under My Vehicle in iDrive: boost, air-fuel ratio, ignition timing, intake air, gearbox, rail pressure, oil pressure and coolant. Restart the head unit after installing it to see it. -->
<!-- Flare Drive shiftdeck: The first test build. ShiftDeck shows a full-width rev bar with a shift light, and road speed, under My Vehicle in iDrive. Restart the head unit after installing it to see it. -->
<!-- Flare Drive bm3-maps: The first test build. BM3 Maps switches between the four map slots of a bootmod3 multi-map tune from My Vehicle in iDrive, and shows which one is active. It changes nothing unless the engine computer runs a bootmod3 CustomROM it knows. Pick only maps made for the fuel in the tank. -->

- **BoostDeck, ShiftDeck and BM3 Maps for the EntryNav2.** Ported from the NBT Evo apps of
  inotbarnacle/ID6_apps_prerelease. Each is its own plugin: install any of them, by hand or with Flare Drive.
- **BM3 Maps replaces MHD Maps.** It switches bootmod3 map slots and reads the active one back from the engine computer.
- **The apps find the car's diagnostic gateway themselves.** Install does it, and `setup.sh gateway` does it again.
