# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PawPoller desktop app.

Cross-platform: the hidden-imports block branches on sys.platform so
Windows builds pull in pystray._win32 + winotify, Linux builds pull in
pystray._appindicator (and the GTK backend), and macOS (when shipped)
will use pystray._darwin. CI's runners-os matrix picks the right
spec implicitly because PyInstaller is invoked from the relevant OS.
"""

import glob
import os
import sys

block_cipher = None

# Bundle EVERY database/*.sql schema automatically. This used to be a hand-maintained
# list and it silently rotted: each new platform/module adds a schema that init_db()
# reads at startup, and forgetting to add its line here ships an EXE that crashes on
# first run (posts_schema.sql was the one that got missed -> FileNotFoundError). Glob
# so the spec can never fall behind the source tree again. SPECPATH is the spec's dir,
# so this is correct regardless of the build's working directory.
_DB_SCHEMAS = sorted(
    (path, 'database')
    for path in glob.glob(os.path.join(SPECPATH, 'database', '*.sql'))
)

# Per-OS hidden imports — pystray has a different backend per platform
# and PyInstaller can't autodetect which one will be loaded at runtime.
_PLATFORM_HIDDEN_IMPORTS = {
    'win32':  ['pystray._win32', 'winotify'],
    'linux':  ['pystray._appindicator', 'pystray._gtk'],
    'darwin': ['pystray._darwin'],
}
_platform_key = 'linux' if sys.platform.startswith('linux') else sys.platform
_platform_hidden = _PLATFORM_HIDDEN_IMPORTS.get(_platform_key, [])

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('frontend', 'frontend'),
        *_DB_SCHEMAS,
        ('assets', 'assets'),
        ('CHANGELOG.md', '.'),   # served by /api/whatsnew for the in-app "What's new" popup
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'apscheduler.schedulers.asyncio',
        'apscheduler.triggers.interval',
        'PIL',
        # Dashboard auth libs — imported lazily inside functions, so the static
        # scan can miss them; pin them explicitly (gap-wave-4).
        'bcrypt',
        'pyotp',
        'itsdangerous',
        *_platform_hidden,
    ],
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
    [],
    exclude_binaries=True,
    name='PawPoller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # hide console window in release build
    icon='assets/pawpoller.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PawPoller',
)
