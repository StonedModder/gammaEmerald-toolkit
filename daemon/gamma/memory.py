"""Gamma Emerald bot core: UE5.3 runtime memory access via ctypes.
No external deps. Works against the Development build.
"""
import ctypes
from ctypes import wintypes
import struct
import sys

PROCESS_ALL_ACCESS = 0x001F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_PRIVATE = 0x20000
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
WRITABLE = {
    PAGE_READWRITE, PAGE_WRITECOPY,
    PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
}
READABLE = WRITABLE | {PAGE_READONLY, PAGE_EXECUTE_READ}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

OpenProcess = kernel32.OpenProcess
OpenProcess.restype = wintypes.HANDLE
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.restype = wintypes.BOOL
ReadProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]

WriteProcessMemory = kernel32.WriteProcessMemory
WriteProcessMemory.restype = wintypes.BOOL
WriteProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]

VirtualAllocEx = kernel32.VirtualAllocEx
VirtualAllocEx.restype = wintypes.LPVOID
VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                           wintypes.DWORD, wintypes.DWORD]

VirtualFreeEx = kernel32.VirtualFreeEx
VirtualFreeEx.restype = wintypes.BOOL
VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                          wintypes.DWORD]

VirtualProtectEx = kernel32.VirtualProtectEx
VirtualProtectEx.restype = wintypes.BOOL
VirtualProtectEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]

FlushInstructionCache = kernel32.FlushInstructionCache
FlushInstructionCache.restype = wintypes.BOOL
FlushInstructionCache.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, ctypes.c_size_t]

CloseHandle = kernel32.CloseHandle

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", wintypes.LPVOID),
        ("AllocationBase", wintypes.LPVOID),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


VirtualQueryEx = kernel32.VirtualQueryEx
VirtualQueryEx.restype = ctypes.c_size_t
VirtualQueryEx.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]


class GameProcess:
    def __init__(self, pid=None, name="PokemonEmerald"):
        self.pid = pid
        self.name = name
        self.handle = None
        self.module_base = 0
        self.module_size = 0

    def attach(self, pid=None):
        if pid:
            self.pid = pid
        if self.pid is None:
            found = list_named_processes(self.name)
            if not found:
                raise RuntimeError(f"process {self.name} not found")
            self.pid = found[0]["pid"]
        # ALL_ACCESS: encounter hooks need VirtualProtectEx on executable pages.
        self.handle = OpenProcess(PROCESS_ALL_ACCESS, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._get_modules()
        return self

    def _get_modules(self):
        # enumerate modules via Toolhelp32
        me32 = MODULEENTRY32()
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap == -1:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            me32.dwSize = ctypes.sizeof(MODULEENTRY32)
            if kernel32.Module32First(snap, ctypes.byref(me32)):
                while True:
                    modname = me32.szModule.decode(errors="replace")
                    if modname.lower().startswith(self.name.lower()):
                        self.module_base = me32.modBaseAddr
                        self.module_size = me32.modBaseSize
                        break
                    if not kernel32.Module32Next(snap, ctypes.byref(me32)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        print(f"module base = 0x{self.module_base:X} size=0x{self.module_size:X}", file=sys.stderr)

    def rpm(self, addr, size):
        # robust multi-page read
        out = bytearray()
        cur = addr
        remaining = size
        while remaining > 0:
            buf = ctypes.create_string_buffer(remaining)
            read = ctypes.c_size_t(0)
            if not ReadProcessMemory(self.handle, wintypes.LPCVOID(cur), buf, remaining, ctypes.byref(read)):
                return bytes(out) if out else None
            if read.value == 0:
                return bytes(out) if out else None
            out += buf.raw[:read.value]
            cur += read.value
            remaining -= read.value
        return bytes(out)

    def alloc(self, size):
        """Allocate memory in the target process."""
        addr = VirtualAllocEx(self.handle, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not addr:
            raise ctypes.WinError(ctypes.get_last_error())
        return addr

    def free(self, addr):
        VirtualFreeEx(self.handle, addr, 0, 0x8000)  # MEM_RELEASE

    def wpm(self, addr, data):
        """Write process memory (multi-page safe)."""
        buf = ctypes.create_string_buffer(bytes(data))
        cur = addr
        remaining = len(data)
        off = 0
        written_total = 0
        while remaining > 0:
            written = ctypes.c_size_t(0)
            if not WriteProcessMemory(self.handle, wintypes.LPVOID(cur),
                                      ctypes.cast(ctypes.byref(buf, off), wintypes.LPCVOID),
                                      remaining, ctypes.byref(written)):
                return written_total
            if written.value == 0:
                return written_total
            cur += written.value
            off += written.value
            remaining -= written.value
            written_total += written.value
        return written_total

    def read_u64(self, addr):
        b = self.rpm(addr, 8)
        return struct.unpack("<Q", b)[0] if b else 0

    def read_u32(self, addr):
        b = self.rpm(addr, 4)
        return struct.unpack("<I", b)[0] if b else 0

    def regions(self, min_addr=0):
        addr = min_addr
        mbi = MEMORY_BASIC_INFORMATION()
        while True:
            n = VirtualQueryEx(self.handle, wintypes.LPCVOID(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if n == 0:
                break
            ba = mbi.BaseAddress or 0
            if mbi.State == 0x1000:  # MEM_COMMIT
                yield ba, mbi.RegionSize, mbi.Protect, mbi.Type
            nxt = ba + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

    def writable_private_regions(self):
        """Committed private RW heap — party records, species DB, money qword."""
        for ba, size, protect, typ in self.regions():
            if (typ == MEM_PRIVATE
                    and (protect & 0xFF) in WRITABLE
                    and not (protect & PAGE_GUARD)):
                yield ba, size

    def write_code(self, address, data):
        """Write executable bytes: flip the page, write, restore, flush I-cache."""
        data = bytes(data)
        old = wintypes.DWORD()
        if not VirtualProtectEx(self.handle, wintypes.LPVOID(address), len(data),
                                PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            n = self.wpm(address, data)
            if n != len(data):
                raise RuntimeError(f"short code write at {address:#x}")
            FlushInstructionCache(self.handle, wintypes.LPCVOID(address), len(data))
        finally:
            ignored = wintypes.DWORD()
            VirtualProtectEx(self.handle, wintypes.LPVOID(address), len(data),
                             old.value, ctypes.byref(ignored))

    def allocate_near(self, site, size=0x1000):
        """RWX cave within rel32 of `site` so a 5-byte JMP can reach it."""
        granularity = 0x10000
        center = site & ~(granularity - 1)
        for delta in range(granularity, 0x70000000, granularity):
            for candidate in (center + delta, center - delta):
                if candidate <= 0:
                    continue
                result = VirtualAllocEx(
                    self.handle, wintypes.LPVOID(candidate), size,
                    MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
                if result:
                    return int(result)
        raise RuntimeError("could not allocate a code cave within rel32 range")

    def close(self):
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def list_named_processes(name="PokemonEmerald"):
    """Running processes whose exe starts with `name`, biggest working set first.

    Uses Toolhelp, not tasklist: French Windows prints memory as '4 888 832 Ko'
    and decoding that as UTF-8 made attach fail with a parse error.
    """
    prefix = name.lower()
    if prefix.endswith(".exe"):
        prefix = prefix[:-4]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snap, ctypes.byref(pe)):
            return []
        while True:
            exe = pe.szExeFile.decode("utf-8", "replace").lower()
            if exe.startswith(prefix):
                pid = int(pe.th32ProcessID)
                out.append({"pid": pid, "mem_mb": _working_set_mb(pid)})
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    finally:
        CloseHandle(snap)
    out.sort(key=lambda p: -p["mem_mb"])
    for p in out:
        p["likely_game"] = p["mem_mb"] > 500
    return out


def _working_set_mb(pid: int) -> int:
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        handle = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize) // (1024 * 1024)
    finally:
        CloseHandle(handle)


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_size_t),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32First.restype = wintypes.BOOL
kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Process32Next.restype = wintypes.BOOL
kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Module32First.restype = wintypes.BOOL
kernel32.Module32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
kernel32.Module32Next.restype = wintypes.BOOL
kernel32.Module32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
