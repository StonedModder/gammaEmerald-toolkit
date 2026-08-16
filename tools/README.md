# tools/

Two third-party binaries make the asset features work. **Neither is included
here** — both are proprietary and not mine to redistribute. Everything else in
the toolkit works without them.

## `oodle-data-shared.dll` — needed to read the Early Access pak

The EA `.pak` is Oodle-compressed, so previews and extraction need Oodle to
decompress a block. Without it the Assets tab still lists all 404k entries and
search works, but opening an asset reports what is missing.

Oodle ships with Unreal Engine. Copy the DLL out of an engine install:

```
<UE install>/Engine/Binaries/ThirdParty/Oodle/Win64/oodle-data-shared.dll
```

Then either drop it here (`tools/oodle-data-shared.dll`) or point at it:

```bat
set GAMMA_OODLE_DLL=C:\path\to\oodle-data-shared.dll
```

The game itself does not ship a usable copy — UE 5.6 links Oodle statically
into the exe, so there is nothing to borrow from your install.

## `binkadec/binkadec.exe` — needed to export audio

Cries, music and SFX are cooked as Bink Audio 2. `binkadec` decodes them to WAV,
and ffmpeg turns that into MP3. Without it, sprites and animations are
unaffected; audio preview and export report what is missing.

binkadec is part of the RAD Video Tools / Bink SDK. Put it at
`tools/binkadec/binkadec.exe` with its runtime DLLs beside it.

## ffmpeg — needed for GIF and MP3

Must be on `PATH`. Used for GIF encoding (`palettegen`/`paletteuse`), DXT
decoding via a DDS wrapper, and MP3 transcoding.

```bat
winget install Gyan.FFmpeg
```
