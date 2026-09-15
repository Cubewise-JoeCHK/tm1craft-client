# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the tm1craft-client Windows drop-in (#12).
#
# Onedir, console, name "tm1craft-client". Built by
# .github/scripts/build-windows-exe.sh, which stages a README.txt beside
# the exe and zips the folder — a self-contained CLI that needs no
# Python on the target box. The client is pure Python with one static
# dependency tree (TM1py at cli.py import, picked up by analysis), so
# the only lookup analysis cannot see is the ``--version`` probe's
# importlib.metadata read — copy_metadata covers it.
#
# The same spec builds a linux onedir for local validation —
# PyInstaller is not cross-platform, the spec is.

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("tm1craft-client")
binaries = []
hiddenimports = []

a = Analysis(
    [str(Path(SPECPATH) / "tm1craft_client_entry.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="tm1craft-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tm1craft-client",
)
