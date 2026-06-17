# -*- mode: python ; coding: utf-8 -*-
import sys

sys.setrecursionlimit(sys.getrecursionlimit() * 5)

datas = [
    ('backup_search_desktop.py', '.'),
    ('backup_search_app.py', '.'),
    ('main.py', '.'),
    ('config.py', '.'),
    ('config.example.json', '.'),
    ('decrypt_db.py', '.'),
    ('export_all_chats.py', '.'),
    ('chat_export_helpers.py', '.'),
    ('mcp_server.py', '.'),
    ('decode_image.py', '.'),
    ('decode_transfer.py', '.'),
    ('key_scan_common.py', '.'),
    ('key_utils.py', '.'),
    ('find_all_keys.py', '.'),
    ('find_all_keys_windows.py', '.'),
    ('find_all_keys_linux.py', '.'),
    ('find_image_key.py', '.'),
    ('find_image_key_monitor.py', '.'),
    ('wechat_process_check.py', '.'),
    ('emoticons.py', '.'),
    ('wxwork_crypto.py', '.'),
]

hiddenimports = [
    'argparse', 'csv', 'glob', 'hashlib', 'hmac', 'http.server', 'json',
    'platform', 'queue', 'sqlite3', '_sqlite3', 'subprocess', 'tempfile',
    'threading', 'urllib.parse', 'uuid', 'wave', 'xml.etree.ElementTree',
    'tkinter', '_tkinter', 'tkinter.ttk', 'tkinter.messagebox',
    'Crypto', 'Crypto.Cipher', 'Crypto.Cipher.AES', 'Crypto.Util.Padding',
    'zstandard',
]

excludes = [
    'IPython', 'jedi', 'pytest', 'py', 'pylint', 'astroid',
    'matplotlib', 'numpy', 'pandas', 'scipy', 'sklearn',
    'torch', 'tensorflow', 'transformers', 'datasets', 'spacy', 'thinc',
    'boto3', 'botocore', 'grpc', 'uvicorn', 'fastapi',
    'openai', 'whisper', 'pysilk', 'pilk',
]

a = Analysis(
    ['wechat_backup_search_launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WeChatBackupSearchDebug',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
