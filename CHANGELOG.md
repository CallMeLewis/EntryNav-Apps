# Changelog

Each release's section holds one Flare Drive note per plugin, the paragraph the app shows with that plugin's release,
as `<!-- Flare Drive boostdeck: ... -->`, `<!-- Flare Drive shiftdeck: ... -->` and `<!-- Flare Drive bm3-maps: ... -->`.
`tools/publish_release.py publish` refuses a version without the notes of the plugins it publishes.

Each version says whether it is verified or unverified. Verified means this exact build has run in a car.

## [0.1.0] · Unverified

<!-- Flare Drive boostdeck: The first stable release. BoostDeck shows live engine readings under My Vehicle in iDrive: boost, air-fuel ratio, ignition timing, intake air, gearbox, rail pressure, oil pressure and coolant, in Celsius or Fahrenheit. Restart the head unit after installing it to see it. -->
<!-- Flare Drive shiftdeck: The first stable release. ShiftDeck shows a full-width rev bar with a shift light, and road speed, under My Vehicle in iDrive. Restart the head unit after installing it to see it. -->
<!-- Flare Drive bm3-maps: The first stable release. BM3 Maps switches between the four map slots of a bootmod3 multi-map tune from My Vehicle in iDrive, and shows which one is active, on MG1 and F-series engine computers. It changes nothing unless the engine computer runs a bootmod3 CustomROM it knows. Pick only maps made for the fuel in the tank. -->

- **BoostDeck, ShiftDeck and BM3 Maps for the EntryNav2.** Ported from the NBT Evo apps of
  inotbarnacle/ID6_apps_prerelease. Each is its own plugin: install any of them, by hand or with Flare Drive.
- **The apps find the car's diagnostic gateway themselves**, on the link-local network or on the network that
  tethering on the ENET cable hands out, and find it again when it moves. Install does it, and `setup.sh gateway` does
  it again.
- **BoostDeck has a Temperature setting**, Celsius or Fahrenheit.
- **BM3 Maps replaces MHD Maps.** It switches bootmod3 map slots and reads the active one back, on MG1 and F-series
  engine computers, as on the M140i's B58.
