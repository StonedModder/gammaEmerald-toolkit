"""Look at the game's window, so the UI can be driven at any resolution.

WHY THIS EXISTS: the menus are mouse-driven, and the click targets used to be
stored as fractions of the client area. That only worked at the resolution they
were measured at. Two builds side by side settle the point -- the title menu is
a block of four rows anchored top-left, and it is neither fixed-size nor
proportional:

    Early Access, 1920x1080 client:  block at y 146..448, rows 76 px
    Bug fix,      4320x2430 client:  block at y  31..384, rows 88 px

So a fraction that hits "Continue" on one build hits "Options" on the other, and
"Close Game" is two rows further down -- which is how a stray click could quit
the game. Rather than guess, this reads the window and finds the rows.

Capture is PrintWindow into a memory DC: it works on an unfocused, occluded, or
fullscreen window and never steals focus, which the whole toolkit depends on.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return (0, 0)
    return (r.right, r.bottom)


def capture(hwnd: int) -> tuple[int, int, bytes] | None:
    """(width, height, BGRA rows top-down) of the client area, or None."""
    w, h = client_size(hwnd)
    if w <= 0 or h <= 0:
        return None
    src = user32.GetDC(wintypes.HWND(hwnd))
    if not src:
        return None
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # PW_RENDERFULLCONTENT gets the composited surface even when another
        # window is on top; a plain screen grab would return whatever covers it.
        if not user32.PrintWindow(wintypes.HWND(hwnd), mem, PW_RENDERFULLCONTENT):
            gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY)

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h              # negative: top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS):
            return None
        return w, h, buf.raw
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(wintypes.HWND(hwnd), src)


def _row_brightness(w: int, h: int, pixels: bytes, x0: int, x1: int,
                    y0: int, y1: int, step: int = 8) -> list[float]:
    """Mean luminance per row over a column range, sampling every `step` px."""
    out = []
    for y in range(y0, min(y1, h)):
        base = y * w * 4
        total = n = 0
        for x in range(x0, min(x1, w), step):
            i = base + x * 4
            total += pixels[i] + pixels[i + 1] + pixels[i + 2]
            n += 1
        out.append(total / (3 * n) if n else 255.0)
    return out


def rows_from_pixels(w: int, h: int, px: bytes, dark: float = 110.0,
                     min_height: int = 20) -> list[tuple[int, int]]:
    """The row-finding itself, so it can be checked against saved captures."""
    # Search the top half. A third was not enough: at 1080 the menu block ends
    # around y=450, so the last entry was cut off and the block came back the
    # wrong shape.
    limit = max(200, h // 2)
    lum = _row_brightness(w, h, px, int(w * 0.02), int(w * 0.22), 0, limit)
    bands, start = [], None
    for y, v in enumerate(lum):
        if v < dark and start is None:
            start = y
        elif v >= dark and start is not None:
            if y - start >= min_height:
                bands.append((start, y))
            start = None
    if start is not None and len(lum) - start >= min_height:
        bands.append((start, len(lum)))
    if not bands:
        return []

    # The entries can come through either as four separate dark bars (bright
    # gaps between them) or as one dark block divided by thin lighter lines,
    # depending on how the menu is drawn at that size.
    if len(bands) == 4:
        heights = [b - a for a, b in bands]
        if max(heights) <= min(heights) * 1.6:
            return bands

    def split(top: int, bottom: int) -> list[tuple[int, int]]:
        """Four even rows out of one dark block, or [] if it is not that."""
        height = bottom - top
        inner = lum[top:bottom]
        if inner:
            floor, ceiling = min(inner), max(inner)
            if ceiling - floor > 8:
                cut = floor + (ceiling - floor) * 0.55
                seps, run = [], None
                for i, v in enumerate(inner):
                    if v > cut and run is None:
                        run = i
                    elif v <= cut and run is not None:
                        seps.append((run + i) // 2)
                        run = None
                edges = [0] + [x for x in seps if 20 < x < height - 20] + [height]
                rows = [(top + edges[i], top + edges[i + 1]) for i in range(len(edges) - 1)]
                rows = [r for r in rows if r[1] - r[0] >= min_height]
                if len(rows) == 4:
                    hs = [b - a for a, b in rows]
                    if max(hs) <= min(hs) * 1.6:
                        return rows
        row = height / 4
        if 40 <= row <= 300:
            return [(int(top + i * row), int(top + (i + 1) * row)) for i in range(4)]
        return []

    # Take the highest band that actually splits into four even entries. Picking
    # the largest dark band instead matched a slab of artwork further down the
    # screen once the search window was widened.
    for top, bottom in sorted(bands, key=lambda b: b[0]):
        rows = split(top, bottom)
        if rows:
            return rows
    return []


def menu_rows(hwnd: int, dark: float = 110.0, min_height: int = 20) -> list[tuple[int, int]]:
    """Rows of the title menu, top to bottom, as (top, bottom) in client pixels.

    The menu is a block of dark bars pinned to the top-left. Its position and
    row height differ per build and per resolution, so it is measured rather
    than assumed. Returns [] if nothing menu-shaped is there.
    """
    shot = capture(hwnd)
    if not shot:
        return []
    return rows_from_pixels(*shot, dark=dark, min_height=min_height)


def row_brightness(hwnd: int, rows: list[tuple[int, int]]) -> list[float]:
    """Mean luminance of each row, used to tell which one is highlighted."""
    shot = capture(hwnd)
    if not shot:
        return []
    w, h, px = shot
    out = []
    for top, bottom in rows:
        vals = _row_brightness(w, h, px, int(w * 0.02), int(w * 0.22), top + 2, bottom - 2)
        out.append(sum(vals) / len(vals) if vals else 0.0)
    return out


def looks_like_menu(hwnd: int, rows: list[tuple[int, int]]) -> bool:
    """Do these rows really look like the four menu bars?

    All four are dark; only the hovered one lifts a little. A detection that
    ran off the bottom of the menu produced 47/54/56/96 -- the last "row" was
    the background -- and the brightest-row rule then pointed at nothing.
    """
    if len(rows) != 4:
        return False
    lums = row_brightness(hwnd, rows)
    if len(lums) != 4 or min(lums) <= 0:
        return False
    return max(lums) <= min(lums) * 1.6


def highlighted_row(hwnd: int, rows: list[tuple[int, int]], margin: float = 6.0) -> int:
    """Index of the visibly highlighted row, or -1 if none stands out.

    The hovered entry is drawn lighter than its neighbours. Requiring a clear
    winner is what stops a mis-aimed click from being confirmed: without it,
    sending the confirm key anyway once started a New Game and overwrote a save.
    """
    lums = row_brightness(hwnd, rows)
    if len(lums) < 2:
        return -1
    order = sorted(range(len(lums)), key=lambda i: lums[i], reverse=True)
    best, second = order[0], order[1]
    return best if lums[best] - lums[second] >= margin else -1


def first_menu_row_point(hwnd: int) -> tuple[int, int] | None:
    """A point inside the top menu entry (Continue), or None if not visible."""
    rows = menu_rows(hwnd)
    if not rows:
        return None
    top, bottom = rows[0]
    w, _h = client_size(hwnd)
    return (max(40, int(w * 0.05)), (top + bottom) // 2)
