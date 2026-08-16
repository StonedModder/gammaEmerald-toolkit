"""Background input to the game window via PostMessage.

VERIFIED against the EA build: posting WM_KEYDOWN/WM_KEYUP to the game's HWND is
consumed by the game. Proof — from a standing start, PostMessage(VK_RETURN) created
a `W_GE_OW_DIalogue_C` widget, and a following PostMessage(VK_SPACE) created two
`W_GE_ChoiceButton_C` widgets. Nothing else touched the process in between.

Why PostMessage rather than SendInput/keybd_event: it does not steal focus, so a
hunt can run for hours while the machine stays usable. keybd_event requires the
game to be foreground and any stray click desyncs the loop.

Caveat worth knowing: the pawn uses UE5 EnhancedInput. Posted messages reach the
Slate/UMG layer reliably (menus, dialogue, choices — everything the starter hunt
needs). Gameplay movement may not respond the same way; do not assume walking works
without testing it on the specific build.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
# Must happen before any GetClientRect / mouse coordinate maths. Without it Windows
# virtualises the numbers (853x480 instead of the real 1920x1080 client), and every
# posted click lands in the wrong place on a scaled display.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_size_t]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint

WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
MK_LBUTTON = 0x0001
MAPVK_VK_TO_VSC = 0
# lParam bit 29 = "ALT was down". If that bit is set, Unreal treats Enter as
# Alt+Enter and toggles fullscreen. The portable daemon used to trip this.
KF_ALTDOWN = 0x20000000


def key_lparam(vk: int, *, up: bool = False, repeat: bool = False) -> int:
    """WM_KEY* lParam with a real scan code and the ALT context bit forced off."""
    sc = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFF
    lp = 1 | (sc << 16)
    if repeat:
        lp |= 0x40000000
    if up:
        lp |= 0xC0000000
    return lp & ~KF_ALTDOWN


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12

VK = {
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "r": 0x52, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "enter": 0x0D, "space": 0x20, "esc": 0x1B, "back": 0x08, "tab": 0x09,
    "a": 0x41, "b": 0x42, "e": 0x45, "x": 0x58, "z": 0x5A,
    # movement is WASD -- the arrows drive menus only, and an arrow held
    # against IA_PlayerMovement moves the player nowhere at all
    "w": 0x57, "s": 0x53, "d": 0x44, "q": 0x51, "f": 0x46,
    "f1": 0x70, "tilde": 0xC0,
}

# scan-code bits Windows normally sets. Some UI layers check the
# transition-state bit on KEYUP, so send a realistic lParam rather than 0.
# Do NOT use a hardcoded 0x00000001 — that has scan code 0, and a console-subsystem
# poster (the portable daemon) can have Windows fill in the ALT context bit.


def find_window(pid: int, title_contains: str = "Gamma") -> int:
    """HWND of the game's top-level window for `pid`.

    The EA build's title is doubled ("Pokemon Gamma Emerald Pokemon Gamma Emerald "),
    so match on a substring, never on equality -- an exact FindWindowW for
    "Pokemon Gamma Emerald" silently fails on EA.

    Exclusive fullscreen sometimes reports IsWindowVisible=False on the real
    UnrealWindow; still pick it if it is the only / largest client for the pid.
    """
    scored = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        r = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        title = buf.value
        klass = cls.value
        visible = bool(user32.IsWindowVisible(hwnd))
        hit_title = title_contains.lower() in title.lower()
        hit_cls = "unreal" in klass.lower()
        if not (hit_title or hit_cls or area > 10000):
            return True
        score = (2 if hit_title else 0) + (1 if hit_cls else 0) + (1 if visible else 0)
        scored.append((score, area, hwnd))
        return True

    user32.EnumWindows(cb, 0)
    if not scored:
        return 0
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


class GameInput:
    def __init__(self, hwnd: int, pid: int = 0, key_delay=0.06, settle=0.25):
        self.pid = pid
        self.hwnd = hwnd
        self.key_delay = key_delay
        self.settle = settle
        if not hwnd and pid:
            self.refresh()
        if not self.hwnd:
            raise ValueError("no game window")

    def refresh(self) -> int:
        if self.pid:
            h = find_window(self.pid, "Gamma")
            if h:
                self.hwnd = h
        return self.hwnd

    def _post(self, msg, vk, lparam):
        if not user32.PostMessageW(self.hwnd, msg, vk, lparam):
            self.refresh()
            if not user32.PostMessageW(self.hwnd, msg, vk, lparam):
                raise OSError(f"PostMessage failed: {ctypes.get_last_error()}")

    def release_alt(self):
        """Drop Alt/F11 on the game window so the next Enter cannot be Alt+Enter.

        Gamma Emerald binds Alt+Enter and F11 to fullscreen. A console-subsystem
        poster (PyInstaller console=True) and a stuck VK_MENU from Electron's
        hidden menu bar both make a posted Enter toggle fullscreen, after which
        the hunt cannot see widgets or land clicks.
        """
        for vk in (0x12, 0xA4, 0xA5, 0x7A):  # MENU, LMENU, RMENU, F11
            lp = key_lparam(vk, up=True)
            self._post(WM_SYSKEYUP, vk, lp)
            self._post(WM_KEYUP, vk, lp)

    def tap(self, key: str, settle: float | None = None):
        """Press and release one key."""
        vk = VK.get(key.lower())
        if vk is None:
            raise KeyError(f"unknown key {key!r}")
        self.release_alt()
        self._post(WM_KEYDOWN, vk, key_lparam(vk))
        time.sleep(self.key_delay)
        self._post(WM_KEYUP, vk, key_lparam(vk, up=True))
        time.sleep(self.settle if settle is None else settle)

    def hold(self, key: str, seconds: float, repeat_hz: float = 20.0):
        """Hold a key down, sending autorepeat like Windows would."""
        vk = VK.get(key.lower())
        if vk is None:
            raise KeyError(f"unknown key {key!r}")
        self.release_alt()
        self._post(WM_KEYDOWN, vk, key_lparam(vk))
        t_end = time.time() + seconds
        while time.time() < t_end:
            self._post(WM_KEYDOWN, vk, key_lparam(vk, repeat=True))
            time.sleep(1.0 / repeat_hz)
        self._post(WM_KEYUP, vk, key_lparam(vk, up=True))
        time.sleep(self.settle)

    def chord(self, modifier: str, key: str, settle: float | None = None):
        """Modifier + key, e.g. chord("shift", "r") for the in-game soft reset.

        Order matters: the modifier must be down before the key's WM_KEYDOWN and
        released after the key's WM_KEYUP, or the game sees a bare keypress.
        """
        mod = VK.get(modifier.lower())
        vk = VK.get(key.lower())
        if mod is None or vk is None:
            raise KeyError(f"unknown chord {modifier}+{key}")
        self._post(WM_KEYDOWN, mod, key_lparam(mod))
        time.sleep(0.04)
        self._post(WM_KEYDOWN, vk, key_lparam(vk))
        time.sleep(self.key_delay)
        self._post(WM_KEYUP, vk, key_lparam(vk, up=True))
        time.sleep(0.04)
        self._post(WM_KEYUP, mod, key_lparam(mod, up=True))
        time.sleep(self.settle if settle is None else settle)

    # NOTE: there is deliberately no focus()/SendInput path here.
    # SendInput goes to whatever window has focus, so using it means bringing the
    # game forward and taking the keyboard away from whatever the user is doing.
    # Everything below posts to the window instead, which works while the game is
    # in the background. Nothing in the toolkit needed raw input in the end -- the
    # SHIFT+R soft reset works fine as posted messages -- so the focus-stealing
    # code was removed rather than left lying around to be picked up by accident.
    def bg_chord(self, modifier_vk: int, key: str, settle: float = 0.8) -> bool:
        """Modifier+key WITHOUT stealing focus.

        Enhanced Input does respond to posted keys (that is how Enter and the
        arrows drive the menus), but a chorded binding also checks the modifier's
        *state*, which PostMessage never sets. AttachThreadInput shares our thread
        input state with the game's thread, so SetKeyboardState can make Shift look
        held while the key itself is posted. No SetForegroundWindow, so the machine
        stays usable.
        """
        k32 = ctypes.windll.kernel32
        vk = VK.get(key.lower())
        if vk is None:
            raise KeyError(key)
        game_tid = user32.GetWindowThreadProcessId(self.hwnd, None)
        our_tid = k32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(our_tid, game_tid, True))
        try:
            state = (ctypes.c_ubyte * 256)()
            user32.GetKeyboardState(ctypes.byref(state))
            state[modifier_vk] = 0x80          # modifier held
            state[0xA0] = 0x80                 # VK_LSHIFT too, some code checks it
            user32.SetKeyboardState(ctypes.byref(state))

            self._post(WM_KEYDOWN, modifier_vk, key_lparam(modifier_vk))
            time.sleep(0.05)
            self._post(WM_KEYDOWN, vk, key_lparam(vk))
            time.sleep(self.key_delay)
            self._post(WM_KEYUP, vk, key_lparam(vk, up=True))
            time.sleep(0.05)
            self._post(WM_KEYUP, modifier_vk, key_lparam(modifier_vk, up=True))

            state[modifier_vk] = 0
            state[0xA0] = 0
            user32.SetKeyboardState(ctypes.byref(state))
        finally:
            if attached:
                user32.AttachThreadInput(our_tid, game_tid, False)
        time.sleep(settle)
        return attached

    def bg_hold(self, key: str, seconds: float, repeat_hz: float = 30.0) -> bool:
        """Hold a key down for real, without taking focus.

        Movement is an Enhanced Input **Axis2D** (`IA_PlayerMovement`), and axis
        bindings read the keyboard STATE rather than key-press messages. Posted
        WM_KEYDOWNs therefore drive menus and dialogue perfectly while leaving the
        player standing still -- which is exactly what happened when arrow taps
        moved nothing.

        AttachThreadInput shares our input state with the game's thread so
        SetKeyboardState can make the key genuinely look held, while the posted
        key-repeat keeps message-driven bindings happy too. No
        SetForegroundWindow, so the machine stays usable.
        """
        k32 = ctypes.windll.kernel32
        vk = VK.get(key.lower())
        if vk is None:
            raise KeyError(key)
        game_tid = user32.GetWindowThreadProcessId(self.hwnd, None)
        our_tid = k32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(our_tid, game_tid, True))
        try:
            state = (ctypes.c_ubyte * 256)()
            user32.GetKeyboardState(ctypes.byref(state))
            state[vk] = 0x80
            user32.SetKeyboardState(ctypes.byref(state))

            self._post(WM_KEYDOWN, vk, key_lparam(vk))
            end = time.time() + max(0.0, seconds)
            gap = 1.0 / max(1.0, repeat_hz)
            while time.time() < end:
                # re-assert: the game clears the state as it consumes input
                user32.SetKeyboardState(ctypes.byref(state))
                self._post(WM_KEYDOWN, vk, key_lparam(vk, repeat=True))
                time.sleep(gap)

            state[vk] = 0
            user32.SetKeyboardState(ctypes.byref(state))
            self._post(WM_KEYUP, vk, key_lparam(vk, up=True))
        finally:
            if attached:
                user32.AttachThreadInput(our_tid, game_tid, False)
        return attached

    def sequence(self, keys, settle: float | None = None):
        for k in keys:
            self.tap(k, settle=settle)

    # ------------------------------------------------------------------ mouse
    # The pause menu is an icon grid that ignores posted arrow keys, so the bot
    # needs to click it. Coordinates are CLIENT-relative, which is what the
    # client-area screenshot in calibrate.py produces -- no border/title-bar maths.
    def click(self, x: int, y: int, settle: float | None = None):
        lp = (int(y) << 16) | (int(x) & 0xFFFF)
        self._post(WM_MOUSEMOVE, 0, lp)
        time.sleep(0.03)
        self._post(WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(self.key_delay)
        self._post(WM_LBUTTONUP, 0, lp)
        time.sleep(self.settle if settle is None else settle)

    def move(self, x: int, y: int):
        self._post(WM_MOUSEMOVE, 0, (int(y) << 16) | (int(x) & 0xFFFF))

    def client_size(self) -> tuple[int, int]:
        r = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top

    def alive(self) -> bool:
        return bool(user32.IsWindow(self.hwnd))
