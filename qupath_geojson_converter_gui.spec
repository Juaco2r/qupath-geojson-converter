# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

APP_NAME = "QuPathGeoJSONConverter"
SCRIPT = "qupath_geojson_converter_gui.py"

ICON_ICO = "assets/icon/qupath_geojson_converter.ico"
ICON_ICNS = "assets/icon/qupath_geojson_converter.icns"

# Windows uses .ico. macOS uses .icns. Linux generally ignores the executable icon,
# but keeping the .ico here is harmless.
ICON = ICON_ICNS if sys.platform == "darwin" and Path(ICON_ICNS).exists() else ICON_ICO

block_cipher = None

# Shapely is used by the merge mode. These collection helpers make the
# packaged app more reliable across Windows, macOS, and Linux.
shapely_datas = collect_data_files("shapely")
shapely_binaries = collect_dynamic_libs("shapely")
shapely_hiddenimports = collect_submodules("shapely")

icon_datas = []
if Path(ICON_ICO).exists():
    icon_datas.append((ICON_ICO, "assets/icon"))
if Path(ICON_ICNS).exists():
    icon_datas.append((ICON_ICNS, "assets/icon"))

a = Analysis(
    [SCRIPT],
    pathex=[],
    binaries=shapely_binaries,
    datas=shapely_datas + icon_datas,
    hiddenimports=shapely_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name=f"{APP_NAME}.app",
        icon=ICON_ICNS,
        bundle_identifier="org.juaco2r.qupathgeojsonconverter",
        info_plist={
            "CFBundleName": "QuPath GeoJSON Converter",
            "CFBundleDisplayName": "QuPath GeoJSON Converter",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
        },
    )
