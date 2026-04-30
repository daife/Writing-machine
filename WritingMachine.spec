# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path.cwd()
site_packages = project_root / '.venv' / 'Lib' / 'site-packages'
autotrace_runtime = project_root / 'third_party' / 'autotrace-runtime' / 'bin'

datas = []
binaries = []
hiddenimports = ['serial.tools.list_ports']
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
for package_name in ('vpype', 'vpype_cli', 'vpype_gcode', 'pnoise'):
    tmp_ret = collect_all(package_name)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
if site_packages.exists():
    for mypyc_runtime in site_packages.glob('*mypyc*.pyd'):
        binaries.append((str(mypyc_runtime), '.'))
if autotrace_runtime.exists():
    for runtime_file in autotrace_runtime.iterdir():
        if runtime_file.is_file():
            binaries.append((str(runtime_file), 'third_party/autotrace-runtime/bin'))
    runtime_root = autotrace_runtime.parent
    for runtime_doc in runtime_root.rglob('*'):
        if runtime_doc.is_file() and runtime_doc.parent != autotrace_runtime:
            datas.append((str(runtime_doc), str(runtime_doc.parent.relative_to(project_root))))


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    exclude_binaries=False,
    name='WritingMachine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
