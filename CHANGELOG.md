# Changelog

Each release's section holds one Flare Drive note per plugin, the paragraph the app shows with that plugin's release,
as `<!-- Flare Drive boostdeck: ... -->`, `<!-- Flare Drive shiftdeck: ... -->` and `<!-- Flare Drive bm3-maps: ... -->`.
`tools/publish_release.py publish` refuses a version without the notes of the plugins it publishes.

Each version says whether it is verified or unverified. Verified means this exact build has run in a car.

## [0.1.1] · Verified

<!-- Flare Drive boostdeck: A new look: each reading has its own card, in two rows of four. The gauges update faster too: BoostDeck now reads the engine about every 35 milliseconds while its page is open. -->
<!-- Flare Drive shiftdeck: The rev bar sweeps smoothly in 128 steps and follows the engine more closely. Road speed can show in MPH or km/h: pick one under OPTIONS, Speed. -->
<!-- Flare Drive bm3-maps: BM3 Maps is now Bootmod3 Suite. Larger map tiles, and the active map is marked on its tile. OPTIONS has two new screens: Active Sound and burbles. Like map switching, they write nothing unless the engine computer runs a bootmod3 CustomROM Bootmod3 Suite knows. Burbles are read back from the engine computer after each change. -->
<!-- Flare Drive engine-codes: The first release. Engine Fault Codes reads the engine computer's fault codes under My Vehicle in iDrive, with a description of each when it has one, and clears them. Restart the head unit after installing it to see it. -->

Ported from upstream's 14 November update (inotbarnacle/ID6_apps_prerelease, `697a3fa` to `a402dd0`).

- **Engine Fault Codes, a new plugin.** It reads the DME's fault memory, four codes a page with upstream's
  descriptions, and clears it, retrying the clear in the extended session when the DME asks for that.
- **BoostDeck has cards.** Each reading gets a card in a 4 × 2 grid, after upstream's new layout: the name is drawn in
  the card, with the value under it and a rounded bar at the bottom. The engine readings take the top row.
- **BoostDeck is faster.** The collector reads every 35 ms instead of 150 ms, and the screen takes a frame every 25 ms.
- **ShiftDeck is smoother and faster.** It reads engine speed every 20 ms and road speed every 65 ms. The rev bar has
  128 steps instead of 32 and eases between readings. A new Speed screen picks MPH or km/h, and the choice is kept.
- **BM3 Maps is now Bootmod3 Suite**, as upstream calls it, since it does more than maps. Its plugin, folder,
  worker and Flare Drive feed keep their names, so an install of 0.1.0 updates in place, firewall rule included.
- **Bootmod3 Suite has Active Sound and burbles**, from bootmod3's controls in cauliflowermossgrape's BM3 Live port. Burbles
  write the ROM's timing table and switch and read both back. Upstream's exhaust flap screen is left out: bootmod3
  sets the flap in each map, and the DME lets a one-off flap request go after about 20 seconds.
- **Bootmod3 Suite's tiles are larger and show the active map**, outlined with an ACTIVE tag, whenever the last read
  confirmed it. Any other answer leaves no tile marked.
- **Engine Fault Codes colours each row's mark by its state**: red for Active, amber for Pending, blue for Stored.

## [0.1.0] · Verified

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
