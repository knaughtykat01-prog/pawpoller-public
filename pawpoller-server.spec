"""PyInstaller spec for PawPoller Server — the headless, dockerless build (4.12.0).

Same tree as the desktop spec (frontend, schemas, assets, CHANGELOG) minus everything that
needs a screen: no pywebview, no pystray, no tkinter. A console binary named
`PawPoller-Server` that the installers in installer/server/ run from a service unit.
Built on Linux x86_64, Linux arm64 and Windows x64 by .github/workflows/build.yml.
"""
from pathlib import Path
import glob
import os

block_cipher = None

_DB_SCHEMAS = sorted(
    (path, 'database')
    for path in glob.glob(os.path.join(SPECPATH, 'database', '*.sql'))
)
_CHANGELOG = [('CHANGELOG.md', '.')] if Path('CHANGELOG.md').is_file() else []

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('frontend', 'frontend'),
        *_DB_SCHEMAS,
        ('assets', 'assets'),
        *_CHANGELOG,
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
        'bcrypt',
        'pyotp',
        'itsdangerous',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The desktop-only stack must not be dragged in by a stray import: leaving it out is
    # what makes detect_runtime_mode() answer "server" (it tries `import webview`).
    excludes=['webview', 'pystray', 'tkinter', 'winotify'],
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
    name='PawPoller-Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,      # a service: logs to stdout/journal, no window
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
    name='PawPoller-Server',
)
