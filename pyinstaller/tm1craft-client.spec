# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the tm1craft-client Windows drop-in (#12).
#
# Onefile, console, name "tm1craft-client": a single self-contained
# exe, no _internal/ folder (#22). Built by
# .github/scripts/build-windows-exe.sh, which stages a README.txt and a
# tm1-client.ini.example beside the exe and zips the folder — a CLI
# that needs no Python on the target box. The client is pure Python
# with one static dependency tree (TM1py at cli.py import, picked up by
# analysis), so the only lookup analysis cannot see is the
# ``--version`` probe's importlib.metadata read — copy_metadata covers
# it (the metadata lands inside the bundle).
#
# Onefile tradeoffs accepted (#22): each launch extracts to a temp dir
# (slower startup, first run worst) and onefile exes get flagged by
# some antivirus more often than onedir — fine for an occasional CLI.
#
# The same spec builds a linux onefile for local validation —
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

# Onefile: binaries and datas go inside the exe (exclude_binaries
# False); there is no COLLECT — nothing is written beside the exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name="tm1craft-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
