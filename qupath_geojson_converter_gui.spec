# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

block_cipher = None

# Shapely is used by the merge mode. These collection helpers make the
# packaged app more reliable across Windows, macOS, and Linux.
shapely_datas = collect_data_files("shapely")
shapely_binaries = collect_dynamic_libs("shapely")
shapely_hiddenimports = collect_submodules("shapely")


a = Analysis(
    ["qupath_geojson_converter_gui.py"],
    pathex=[],
    binaries=shapely_binaries,
    datas=shapely_datas,
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
    name="QuPathGeoJSONConverter",
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
)
