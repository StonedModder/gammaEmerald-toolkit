# tools/

Three third-party binaries live here, and all three are bundled into the
portable release so it works with no setup.

`oodle-data-shared.dll` and `binkadec/` are committed; without them a portable
build cannot read the pak or export a cry, and telling every user to go and find
them defeats the point of shipping one exe. ffmpeg is not committed -- it is
115 MB and freely downloadable -- but the build does bundle it, so fetch it
before packaging (see below).

## `ffmpeg.exe` — needed for GIF and MP3 export

Renders animations to GIF, cries and music to MP3, and decodes DXT/BC textures.
Sprites and browsing work without it; the app says so when it is missing.

The portable release ships with it, so users need nothing. For a source
checkout, either install it (`winget install Gyan.FFmpeg`) or drop a build here
as `tools/ffmpeg.exe`. `GAMMA_FFMPEG` overrides the path.

Builds of the release bundle come from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) — use the **LGPL**
variant (`ffmpeg-master-latest-win64-lgpl.zip`), not the GPL one, and keep its
`LICENSE.txt` next to it as `tools/ffmpeg-LICENSE.txt`. Both files are picked up
by the packaging step; without them the build still succeeds but the release
will not have ffmpeg in it.

## `oodle-data-shared.dll` — needed to read the Early Access pak

**Already here and already bundled.** The rest of this section is only for
rebuilding it from an engine install.

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
