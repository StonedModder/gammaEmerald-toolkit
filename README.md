# Gamma Toolkit

<p align="center">
  <a href="https://github.com/StonedModder/gammaEmerald-toolkit/releases"><img src="https://img.shields.io/badge/release-v0.0.2-0ea5e9?style=flat-square" alt="release v0.0.2"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/StonedModder/gammaEmerald-toolkit?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20x64-0078D4?style=flat-square&logo=windows&logoColor=white" alt="Windows x64">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/electron-37-47848F?style=flat-square&logo=electron&logoColor=white" alt="Electron 37">
  <img src="https://img.shields.io/badge/Unreal-5.3%20%2F%205.6-313131?style=flat-square" alt="Unreal Engine 5.3 and 5.6">
</p>

Desktop companion for **Pokémon Gamma Emerald**. One window: a starter shiny hunter that runs in the background, cheats for odds and encounters, save backups, and an asset browser that reads the game's pak in place.

The game does not need to be the active window — you can keep using your computer while it runs.

Fan-made. Reads and writes the memory of a locally running game, and reads that game's pak. **This repository does not include any game content.**

## Features

| | |
|---|---|
| **Starter hunter** | Soft-resets at Birch's bag, reads the roll before you confirm, and keeps going until the starter is shiny. |
| **Encounter odds** | Set wild shiny odds to any `1 in N` — type it, drag it, or pick a preset. Puts the original values back when you're done. |
| **Force starter shiny** | Flips the starter's shiny flag on demand. This is a separate roll from wild odds. |
| **Wild encounter** | Pick any Pokémon and the next wild encounter will be that species, shiny if you want. |
| **Money** | Read and set your money. |
| **Party shiny** | Make a Pokémon in your party shiny. Writes a backup of the party first. |
| **Save backups** | Snapshot your save as often as you like, name them, and restore any one later. |
| **Assets** | Browse everything in the pak — Pokémon, trainers, NPCs, buildings, maps, music, cries. Preview sprites, animations and audio, then extract as PNG, GIF or MP3. |

Typical hunt cycle is about **30 seconds per reset** (~110–125/hour), nearly all of it the game loading your save. Soft reset is `Shift+R`.

## Supported builds

| | Original | Early Access |
|---|---|---|
| Date | 2025-05-31 | 2026-08-15 |
| Engine | UE 5.3.2 | UE 5.6.1 |
| Container | IoStore (unencrypted) | `.pak` v11 (AES-encrypted index) |
| Packages | Zen `.uasset` | legacy `.uasset` + `.uexp` |

Pick your game executable in the app. The Early Access pak key is built in, so there's nothing to configure.

## Install

### Portable release (recommended)

Download **`GammaToolkit-v0.0.2-portable.exe`** from [Releases](https://github.com/StonedModder/gammaEmerald-toolkit/releases) and run it. Nothing to install — no Python, no Node, and ffmpeg is included, so GIF and MP3 export work out of the box.

### From source

Windows only — it uses Win32 memory and input.

- Python 3.11+ on `PATH`
- Node 18+
- [ffmpeg](https://ffmpeg.org/) on `PATH` for GIF/MP3 export (`winget install Gyan.FFmpeg`) — the portable release bundles this, a source checkout does not
- Optional: Oodle DLL and `binkadec` for compressed assets and audio — see [`tools/README.md`](tools/README.md). Not redistributed here.

```bat
launchApp.bat
```

First run installs the Python packages and Electron (~200 MB, once). Or:

```bash
cd app
npm install
npm start
```

## Getting started

1. **Choose game…** and pick your `PokemonEmerald.exe`, or start the game yourself.
2. **Launch & attach**, or **Attach** if it's already running.
3. Load your save.

The dot in the top right turns green when it's connected.

## Hunt tab

For the starter hunt, load a save standing in front of Birch's bag, pick Treecko, Torchic or Mudkip, then **One attempt** to try it once or **Start hunt** to loop until it finds a shiny.

Leave **talk to the bag first** ticked if you're standing in front of the bag. Untick it if the three balls are already on screen.

**Encounter odds** sets the wild shiny rate — anything from `1/1` to `1/65536`, or type your own number. It doesn't affect the starter; use **Force this starter shiny** for that.

## Cheats tab

**Wild encounter** — search for a Pokémon, click it, and flip the switch. The next wild Pokémon you meet will be that species. Tick **always shiny** first if you want it shiny. Turn the switch off once you've caught it.

You don't type anything: the dex number comes from the game itself. Pokémon that this build has no data for are greyed out, since they can't be forced.

**Party shiny** — scan your party, then make a slot shiny. A copy of your party is written to `%LOCALAPPDATA%\GammaToolkit\party_backups` before anything changes.

## Assets tab

Opens the pak and indexes it in a couple of seconds — around 400,000 files on the Early Access build. Nothing is unpacked until you click something.

Pick a category on the left, search, then choose a Pokémon or NPC to see everything it has: idle and attack animations, front and back sprites, shiny variants, cries. Animations play in the panel; audio plays too. **Extract** saves the one you're looking at, **Extract all** saves everything for that subject — sprites as PNG, animations as GIF, sounds as MP3.

Under **Levels** you'll find the game's maps. If you're attached, **Travel here** walks your character into the matching teleport, so you can go and stand in a map you were just looking at.

## Tools tab

**Save backups.** Your save lives in a hidden folder that's easy to lose and impossible to undo by hand, so this copies the whole thing.

- **Back up now** takes a snapshot. Give it a name like *before Petalburg gym* so you can find it again.
- Keep as many as you like — they're listed newest first with the date, file count and size.
- **Restore** puts one back. **Delete** removes a backup and leaves your save alone.

Restoring asks you to confirm first, because it replaces your current save and there's no undo. Your current save is snapshotted automatically before every restore (those show up tagged `auto`), so even a restore you didn't mean is recoverable.

**Close the game before restoring.** While it's running it keeps the save files open, and it writes its own copy back the next time you save — so a restore either gets refused or gets overwritten. Backing up while the game runs is fine.

Backups live in `%LOCALAPPDATA%\GammaToolkit\save_backups`.

**Money.** Shows what you're carrying and lets you set it to anything. Spending in a shop updates it straight away. Back up your save first if you want to be able to put it back.

## Where things are kept

```
%LOCALAPPDATA%\GammaToolkit\
  cache\           rendered previews (safe to delete)
  save_backups\    your save snapshots
  party_backups\   party copies taken before a shiny write
```

## Layout

```
app/          Electron shell (UI only)
daemon/       JSON-RPC daemon
  gamma/      memory, hunter, odds, saves, pak / IoStore, asset decode
tools/        ffmpeg + optional oodle-data-shared.dll / binkadec
```

The UI talks to `daemon/main.py` over JSON-RPC on stdio — there's no HTTP server and nothing listening on a port. Override the interpreter with `GAMMA_PYTHON` if you need to.

## Credits

**[Ionic28](https://github.com/Ionic28)** worked out the Pokémon spawning and encounter cheats, the money cheat, the memory-tools template the daemon is built on, and how to fetch and list the party. Those parts of the toolkit come from that work.

The shiny rate control, forced shiny, map teleporting and the save backup features are separate work and not theirs.

## Disclaimer

Pokémon Gamma Emerald, Pokémon, and related assets belong to their respective owners. This tool is unofficial. Use it only with a game you own, on your own machine.

## License

[MIT](LICENSE) for this toolkit's source.

The portable release bundles **ffmpeg** (LGPL v2.1+, unmodified, from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)); its licence ships beside it as `tools/ffmpeg-LICENSE.txt` and the source is available from [ffmpeg.org](https://ffmpeg.org/download.html). The other third-party binaries described under `tools/` stay under their own licences and are not included.
