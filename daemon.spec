# -*- mode: python ; coding: utf-8 -*-
"""One-file stdio daemon for the portable Electron build.

console=False: a console-subsystem exe makes Windows treat posted Enter as
Alt+Enter (toggle fullscreen) when the hunt opens Birch's bag.
stdin/stdout stay piped from Electron regardless of the subsystem.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hidden = collect_all("numpy")
hidden += collect_submodules("gamma")
hidden += collect_submodules("Crypto")
hidden += [
    "memory",
    "Crypto.Cipher.AES",
    "gamma.memory",
    "gamma.ue",
    "gamma.hunt",
    "gamma.shiny",
    "gamma.input",
    "gamma.pak",
    "gamma.iostore",
    "gamma.assets",
    "gamma.legacy_uasset",
    "gamma.versions",
    "gamma.layouts",
    "gamma.encounter",
    "gamma.party",
    "gamma.money",
    "gamma.nav",
    "gamma.travel",
    "gamma.wild",
    "gamma.resources",
    "gamma.saves",
    "gamma.items",
]

a = Analysis(
    ["daemon/main.py"],
    pathex=["daemon", "daemon/gamma"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gamma-daemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
