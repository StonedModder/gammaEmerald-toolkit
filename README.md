# gammaEmerald-toolkit

Desktop toolkit for **Pokemon Gamma Emerald**: a starter shiny hunter that runs
in the background, live encounter-odds control, and an on-demand asset browser.
One Electron window, one Python daemon, two supported builds. Nothing is
unpacked up front -- point it at the game and open only what you want to see.

> Fan-made tool for a fan game. It reads and writes the memory of a game running
> locally and reads that game's asset pak. **No game content is included here.**

## Requirements

- **Windows** -- the input and memory layers are Win32
- **Python 3.11+** on `PATH` (`numpy`, `pycryptodome` -- installed on first run)
- **Node 18+** for Electron (installed on first run)
- **ffmpeg** on `PATH` for GIF/MP3: `winget install Gyan.FFmpeg`
- Two optional third-party binaries for pak decompression and audio export --
  see [`tools/README.md`](tools/README.md). Not redistributed here.

## Launch

**Double-click `launchApp.bat`.** It checks Python, installs the Python and
Electron dependencies the first time (~200 MB, once), and opens the window.

From a terminal use a path, not the bare name -- on a machine with
`NoDefaultCurrentDirectoryInExePath=1` cmd does not search the current directory
and a bare `launchApp.bat` fails with "is not recognized":

```bat
"C:\path\to\gammaEmerald-toolkit\launchApp.bat"
```

Or run Electron directly:

```bash
cd app
npm install      # first time only
npm start
```

Electron spawns `daemon/main.py` itself and talks **JSON-RPC over stdio**. There
is no HTTP server and no port. Override the interpreter with `GAMMA_PYTHON`.

## Layout

```
app/            Electron — UI only, no game logic
  main.js         spawns the daemon, relays JSON-RPC, no remote content
  preload.js      contextBridge; contextIsolation on, nodeIntegration off
  renderer/       index.html + app.css + app.js
daemon/
  main.py         JSON-RPC line protocol on stdin/stdout
  gamma/
    versions.py   per-build paths, container format, AES key
    layouts.py    per-engine struct offsets  <-- single source of truth
    memory.py     attach, RPM/WPM, region walk
    ue.py         GUObjectArray + NamePool discovery, names, reflection
    shiny.py      Blueprint EX_FloatConst odds scan/patch
    hunt.py       starter hunter: roll detection, forcing, restart loop
    input.py      background key input via PostMessage
    pak.py        pak v11 reader (AES + Oodle)
    iostore.py    IoStore reader for the original build
    assets.py     on-demand browse/preview/extract: PNG, GIF, MP3
    legacy_uasset.py  legacy package + UPaperFlipbook parsing
tools/          where you put oodle-data-shared.dll / binkadec (see its README)
```

## Builds

| | Original | Early Access |
|---|---|---|
| Date | 2025-05-31 | 2026-08-15 |
| Engine | UE 5.3.2 | UE 5.6.1-44394996 |
| Container | IoStore, unencrypted | `.pak` v11, **AES-encrypted index** |
| Packages | Zen `.uasset` | legacy `.uasset` + `.uexp` |
| Content | 94,661 files | 400,999 files |

The EA AES key is compiled into `versions.py`, so decryption needs no setup.
`GAMMA_EA_AES_KEY` or an `ea_aes.key` file still overrides it.

## The shiny mechanism (verified working)

`BP_GE_PickStarterPlayer_C` owns the starter roll. The scene rolls once when the
starter screen loads and publishes the result per species:

```
ShinyFrame       +0x3E0  int    the rolled value (e.g. 1225, 299)
ShinyStarterID   +0x3E4  int    0 Treecko / 1 Torchic / 2 Mudkip
isShinyTreecko?  +0x3E8  bool
isShinyTorchic?  +0x3E9  bool
isShinyMudkip?   +0x3EA  bool
```

Writing `isShinyTreecko? = True` and confirming produced a shiny (teal) Treecko
with the shiny marker on its HP bar. That is the cheat, and the same flags are the
hunt's detector.

Because the flags are readable **before** the pick is confirmed, the bot reads the
roll the instant the screen opens and rejects a dud without ever accepting a
Pokemon — the save is never written.

**Two dead ends, recorded so nobody re-tries them:**

- `BP_Brendan_C.ShinyRate` (+0xA0C, 1023) and `isStarterShiny?` (+0xA31) look
  exactly like the mechanism and are not. Setting ShinyRate to 0 *before* the
  starter scene loaded still produced a normal Treecko and the flag never flipped.
- The `EX_FloatConst 0.01` sites (45 of them, Cheats tab) are wild-encounter odds
  and do not touch the starter.

## Running a hunt

```bash
cd daemon
python hunt_cli.py --starter treecko            # real hunt
python hunt_cli.py --starter mudkip --force     # prove the loop instantly
python hunt_cli.py --starter torchic --max 50
```

Or use the Hunt tab: pick a starter from the roster, **Attach**, **Start hunt**.

Each attempt: load save -> open the bag -> read the roll -> if it is a dud,
soft-reset with **SHIFT+R** and roll again.

**~29-32s per reset (113-125 resets/hour)**, measured over consecutive unattended
attempts on each of the three starters:

| starter | attempts | per reset | resets/hour |
|---|---|---|---|
| Treecko | 5 | 28.7-28.8s | 125 |
| Mudkip | 6 | 28.6-31.5s | 120 |
| Torchic | 5 | 31.6-32.1s | 113 |

Where a cycle goes:

| step | seconds | |
|---|---|---|
| read the roll | 9.3 | open the bag, wait for the scene, read the flags |
| **SHIFT+R** | **0.5** | the soft reset itself is nearly free |
| back to the title | 6.7 | game unloads the world |
| load the save | 11.3 | the game's own load |

Nearly all of it is now the game loading; the bot's own overhead is fractions of
a second. Input is posted to the window, so the game does **not** need focus and
the machine stays usable while a hunt runs.

### Encounter odds

One number — "1 in N" — set by typing, dragging or picking a preset. All three
write the same value, so they cannot disagree. It patches the Blueprint odds
constants **and** `ShinyRate` together, because setting only one leaves the other
contradicting it. Any denominator works, not just the presets.

The slider is **log scaled**. A linear 1..1,000,000 slider spends 99% of its
travel above 1/10,000, which puts every value anyone actually wants (1/512,
1/1024, 1/4096) inside the first pixel — that is why dragging it felt broken.

This is the **wild encounter** roll. The starter is a separate, binary mechanism
(a flag the game sets when the screen opens), so it gets its own button and no
probability. Claiming otherwise in the UI would just be a lie with a slider on it.

### Attach

Attach returns in about **5s**, or instantly on a re-attach. It previously sat on
"discovering" for the better part of a minute because it did three slow things
inline that nothing was waiting on:

| step | cost | now |
|---|---|---|
| find the player pawn | ~24s | background, arrives as an event |
| verify struct offsets | ~17s | background, arrives as an event |
| scan Blueprint odds sites | ~37s | background, arrives as an event |

`status()` is answered from cached state, so polling it never blocks either.

Related: library code printed diagnostics to **stdout**, which is the JSON-RPC
channel. Electron's parser silently dropped the malformed lines, so it looked
fine while quietly corrupting the stream. Diagnostics go to stderr now.

### What the Hunt tab shows

- the odds gauge, log-scaled 1/1 .. 1/8192, gold when forced or found
- a roster of the three starters drawn with the **real game sprites**, animated.
  A card swaps to that species' shiny sprite and gains a count badge once found
- per starter: resets attempted, shinies found, and the attempt number of each
  find (`1 in 1 . @1`)
- resets, elapsed, **resets/hour**, seconds per reset, total shinies
- a per-attempt list and a full run log

Sprites live in `app/renderer/assets/*.gif`, built from the extracted FRONT/Idle
flipbooks (24fps, every other frame) with the same GIF encoder the viewer uses.

### How it re-rolls

The roll only happens on scene load, and re-selecting a ball does not re-roll
(ShinyFrame stays put), so every attempt needs a fresh load.

`SHIFT+R` is the game's own soft reset — `IA_ResetGame` chorded with
`IA_UseKeyItem`. It works from inside the starter scene, where `esc` is swallowed,
and it takes the process straight back to the title in well under a second. That
one change cut the cycle from ~69s to ~29s by removing the relaunch entirely.

Post `VK_LSHIFT` (0xA0) specifically: the generic `VK_SHIFT` (0x10) does not match
the binding and silently does nothing.

Relaunching the process is kept only as the recovery path for when a soft reset
does not take. It refuses to kill the game unless it knows the exe to start again.

### Things that made the loop slow or wrong

Each of these cost real time; none are obvious from the outside.

- **A blind second Enter at the title.** "Continue" was being sent before the main
  menu existed, so it went nowhere and burned a 45-90s timeout every reset.
  Now the loop nudges (re-press + poll) instead of waiting idle: 136s -> 69s, and
  the same idle-wait bug reappeared after the reload path was reworked, doubling
  the reload phase to 86s until the nudge was restored: 86s -> 19s.
- **`_class_ptr` cached misses.** Called during boot, the class did not exist yet,
  the `0` was cached forever, and menu detection never fired again. Misses are now
  remembered for 5s only — long enough to stop a poll loop paying a 6s full scan
  per tick, short enough that a class appearing mid-boot is still seen.
- **`_class_ptr` cached HITS went stale.** A Blueprint class can be collected and
  reloaded across a save load, which moves its `UClass`. The cached pointer then
  matched no object, so every widget check answered False and the title wait
  burned its full 90s timeout. Cached hits are now name-verified before use — one
  read, versus the 258k-object walk it avoids.
- **A class-pointer read per object, per poll.** Finding the pawn meant 24,000
  `ReadProcessMemory` calls. Each object's class is now memoised against its whole
  32-byte `FUObjectItem`, so a recycled array slot misses the cache instead of
  returning the previous occupant's class.
- **Scanning by class NAME in a poll loop.** Finding the starter scene resolved a
  name per object over the whole array. Comparing the class pointer, newest slots
  first, is the same answer for a fraction of the cost.
- **`configure()` cleared `reattach` and `exe_path`.** A stray paste disarmed the
  recovery path every time the starter was switched.
- **`iter_objects` used a stale `NumElements`.** Anything created after a world
  load was invisible, so the player pawn could not be found.
- **`status()` ran a full object scan every poll.** At 1Hz that starved the hunt
  thread and the hunt hung on "waiting for player" forever.
- **The hunter constructor ran a full scan**, blocking the `hunt.start` RPC.
- **Arrow keys work at the ball screen but not in a dialogue.** Opening the bag
  drops you straight into the highlighted starter's dialogue, so the claim has to
  decline back to the ball screen first, then move, then accept. Skipping that
  claimed a plain Treecko during two separate Torchic runs.
- `W_GE_Pause_C` exists even when the pause menu is hidden, so its presence is NOT
  proof the menu is open.

## The Assets tab

Browse the pak without unpacking it. The index is read once (~2s, 404k entries)
the first time the tab is opened; a file is decoded only when you click it.

Three columns narrow the question down: **what kind of thing** (Pokemon,
trainers, NPCs, buildings, cries, music, SFX...), **which one** (searchable),
and **what it looks and sounds like**. Sprites and animations render on a
checkerboard at 1x-8x nearest-neighbour, because every sprite here has an alpha
channel and a flat panel hides the edge pixels you are checking. Sounds play in
place. **Extract** writes the selection to a folder you pick — animations as
GIF, sounds as MP3, sprites as PNG, anything else as the raw package.

Rendered previews are cached under `%LOCALAPPDATA%\GammaToolkit\cache`, so
clicking back to an animation is instant.

### What an "animation" actually is

The **flipbook** (`FB_*`) is the animation, not the folder. It owns the frame
order, the frame runs (a held frame is listed twice) and the FPS. Two shapes
exist in this game and both are handled:

- **battle sprites** (Pokemon, trainers) — one texture per frame, 24fps
- **overworld sprites** (NPCs, player) — one sheet, and each frame is a
  *sub-rectangle* of it. Listing that folder produced a 13-frame "animation" of
  the whole sheet flashing. The flipbook names sub-sprites instead.

Two things made that hard to see:

- Pixels live in a separate **`.ubulk`**, not in the `.uasset`/`.uexp`. Reading
  only the package finds the format string and no pixels.
- An FName is `(index, number)` and the **number carries a trailing `_N`** — UE
  stores `Foo_3` as "Foo" with number 4. Ignoring it collapsed `Sprite_0`
  through `Sprite_11` to one name, so an atlas animation resolved to a single
  frame repeated.

Per-sprite rects live in each `UPaperSprite`'s `BakedRenderData`, which this
build does not serialise, so the grid is derived: **square cells that tile the
sheet, largest size that still fits every sub-sprite**. Sheets are not always
full — NurseJoy is 5 sprites on a 96x96 sheet, a 3x3 grid of 32x32 with four
slots unused. Factorising the sprite *count* instead cannot express that (5 has
no grid dividing 96x96), which is why those NPCs and the Poke Balls showed the
whole sheet.

Four more things had to be right before every asset rendered:

- **Not every texture is BGRA.** Effects and some player sheets are `PF_DXT5`.
  They are wrapped in a DDS header and handed to ffmpeg rather than decoding BC
  by hand — the hand-rolled decoder and ffmpeg produced identical garbage, which
  is how the real bug was found.
- **Inline compressed pixels start at the FRONT of the `.uexp`**, behind a small
  export header, not at the tail like raw BGRA in a `.ubulk`. Slicing the tail
  landed mid-block: recognisable shapes in rainbow noise. The header size varies,
  so the start is located by testing the 16 possible block alignments.
- **Dimensions must be sanity-checked against the payload size.** Taking the last
  `PF_*` match unconditionally read `SPR_Divider` as 1308622848x6647407.
- **Inline BGRA has a trailer too.** The pixels are followed by a few bytes and
  then the 4-byte package tag, so the tail of the `.uexp` is not the end of the
  image. Being 8 bytes out rotated every frame by two pixels: the Poke Balls ran
  off the left edge and the missing strip reappeared on the right, which looked
  exactly like a mis-clipped sprite sheet. The trailer size varies, so the start
  is found by scoring border transparency — and candidates must stay congruent to
  the tail **modulo 4**, or the B/G/R/A channels rotate and everything goes purple.
- **Prefixes lie.** `SPR_GreatballOpen` and `SPR_MagmaSherry_Idle_Down` are
  flipbooks, not textures, and `SPR_B_Normal` is a sprite wrapper pointing at
  `SPR_cursor_fight`. Classifying on the filename marked all of them broken.

That took the failure rate from 94 assets to **6 in 2,486 (0.2%)** — what remains
is two BC5 normal maps and a flipbook whose frames live in another package.

## What is verified against the running EA build

Measured live, not copied from headers; method for each is in `layouts.py`.

- Core discovery works **unchanged** on 5.6 — 240k+ objects, `self_test()` passes
- `FUObjectItem` stride 32, `FNameEntry` packing, `UWorld::PersistentLevel@0x30` — unchanged
- **`ULevel::Actors` moved `0x98` → `0xA0`** — the only confirmed runtime break
- `UStruct::Script@0x60` and `EX_FloatConst=0x1E` — unchanged
- Shiny float `0.01` at 45 sites; write/read/restore round-trip passes
- `BP_GE_PickStarterPlayer_C` starter flags at `+0x3E0..+0x3EA` — forcing one
  produced a real shiny for all three starters (Treecko teal, Torchic gold,
  Mudkip purple)
- The loop re-rolls unattended for **all three starters**, with a distinct roll
  every attempt — Treecko (731, 1407, 1324, 1630, 1653), Mudkip (1706, 1097,
  1973, 1724, 1369, 1412), Torchic (1412, 21, 1408, 1362, 153)
- `SHIFT+R` soft reset returns to the title in **~0.5s**, no relaunch
- Background input via `PostMessage` reaches the game **without focus** — the
  window never comes forward and the machine stays usable
- Assets: 404,134 pak entries indexed in ~2s; sprite PNG, flipbook GIF (both
  per-frame and atlas) and MP3 for cries, music and SFX all decode

## Notes that will save you an hour

- The EA window title is **doubled** (`"Pokemon Gamma Emerald Pokemon Gamma Emerald "`).
  Match it as a substring; an exact `FindWindowW` fails.
- Sampling the first few thousand UObjects finds only **native** functions, whose
  `Script` is empty. That looks exactly like "the offset moved". Filter on
  `class_name(outer) == "BlueprintGeneratedClass"`.
- `iter_objects` must re-read NumElements. discover() captures it once, and a
  stale bound makes every object created after a world load INVISIBLE — the
  player pawn existed but could not be found until this was fixed.
- Widget blueprints are `WidgetBlueprintGeneratedClass`, not
  `BlueprintGeneratedClass`. Filtering on the latter finds no widgets.
- Never run a large memory scan while the game is starting: it peaks near 5 GB
  during PSO precompile and a greedy scanner will OOM it.
- Extracted assets never go in git — they belong in the per-build cache.
