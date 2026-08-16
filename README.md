# Gamma Toolkit

<p align="center">
  <a href="https://github.com/StonedModder/gammaEmerald-toolkit/releases"><img src="https://img.shields.io/badge/release-v0.0.1-0ea5e9?style=flat-square" alt="release v0.0.1"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/StonedModder/gammaEmerald-toolkit?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20x64-0078D4?style=flat-square&logo=windows&logoColor=white" alt="Windows x64">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/electron-37-47848F?style=flat-square&logo=electron&logoColor=white" alt="Electron 37">
  <img src="https://img.shields.io/badge/Unreal-5.3%20%2F%205.6-313131?style=flat-square" alt="Unreal Engine 5.3 and 5.6">
</p>

Desktop companion for **Pokémon Gamma Emerald**. One window: a background starter shiny hunter, live wild-encounter odds, and an on-demand asset browser. Point it at your install — nothing is unpacked until you open it.

Fan-made. Reads and writes memory of a locally running game, and reads that game's pak. **This repository does not include any game content.**

## Features

| | |
|---|---|
| **Starter hunter** | Soft-resets at Birch's bag, reads the roll before you confirm, and keeps going until the target is shiny. Input is posted to the game window, so the process does not need focus. |
| **Encounter odds** | Set wild shiny odds to any `1 in N` (type, slider, or preset). Restores vanilla values when you are done. |
| **Force starter** | Writes the starter shiny flag on demand. Separate from wild odds — the two rolls are not the same mechanism. |
| **Assets** | Browse the pak in place (~400k EA entries indexed in a couple of seconds). Preview sprites, animations, and audio; extract PNG / GIF / MP3. |

Typical hunt cycle is about **30 seconds per reset** (~110–125/hour), mostly the game loading the save. Soft reset is `Shift+R`.

## Supported builds

| | Original | Early Access |
|---|---|---|
| Date | 2025-05-31 | 2026-08-15 |
| Engine | UE 5.3.2 | UE 5.6.1 |
| Container | IoStore (unencrypted) | `.pak` v11 (AES-encrypted index) |
| Packages | Zen `.uasset` | legacy `.uasset` + `.uexp` |

Choose the game executable in the app. The EA pak key is compiled in; `GAMMA_EA_AES_KEY` or an `ea_aes.key` file still overrides it.

## Install

### Portable release (recommended)

Download **`GammaToolkit-v0.0.1-portable.exe`** from [Releases](https://github.com/StonedModder/gammaEmerald-toolkit/releases). No Python or Node required.

### From source

**Windows** only (Win32 memory and input).

- Python 3.11+ on `PATH`
- Node 18+
- [ffmpeg](https://ffmpeg.org/) on `PATH` for GIF/MP3 (`winget install Gyan.FFmpeg`)
- Optional: Oodle DLL and `binkadec` for pak decompression and audio — see [`tools/README.md`](tools/README.md). Not redistributed here.

```bat
launchApp.bat
```

First run installs Python packages and Electron (~200 MB, once). Or:

```bash
cd app
npm install
npm start
```

Override the interpreter with `GAMMA_PYTHON` if needed. The UI talks to `daemon/main.py` over JSON-RPC on stdio — there is no HTTP server.

## Usage

1. **Choose game…** (the `PokemonEmerald.exe` for the build you want) or start the game yourself.
2. **Launch & attach** / **Attach**.
3. Load a save facing Birch's bag.
4. Pick Treecko, Torchic, or Mudkip on the Hunt tab.
5. **One attempt** to test, or **Start hunt** to loop.

Leave **talk to the bag first** checked if you are standing in front of it; uncheck it if the three balls are already on screen.

Wild odds live on the same tab (`1/1` through `1/65536`, or any denominator). That control does not change the starter roll. Use **Force this starter shiny** (or the per-attempt checkbox) for that.

The Assets tab indexes on first open. Extraction writes to a folder you pick. Rendered previews cache under `%LOCALAPPDATA%\GammaToolkit\cache`.

## Layout

```
app/          Electron shell (UI only)
daemon/       JSON-RPC daemon
  gamma/      memory, hunter, odds, pak / IoStore, asset decode
tools/        drop oodle-data-shared.dll / binkadec here (optional)
```

## Disclaimer

Pokémon Gamma Emerald, Pokémon, and related assets belong to their respective owners. This tool is unofficial. Use it only with a game you own, on your own machine.

## License

[MIT](LICENSE) for this toolkit's source. Third-party binaries described under `tools/` remain under their own licenses and are not included.
