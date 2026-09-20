# -*- mode: python ; coding: utf-8 -*-
# stock_svr PyInstaller spec  (빌드: tools\build_server.ps1  또는  pyinstaller --noconfirm stock_svr.spec)
#   dist\stock_svr\stock_svr.exe      GUI (콘솔 창 없음) - 빌드 대상은 이것 하나뿐 (CLI exe 는 만들지 않는다)
#   점검(--check 등)은 개발용 run_stock_svr.bat / python -m stock_svr 로 한다.
# onedir 방식: 실행 속도가 빠르고 백신 오탐이 적다. config\, logs\ 는 exe 옆 폴더를 사용한다.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# anthropic(Claude 거부권 필터)은 일부 모듈을 동적으로 import 하므로 서브모듈을 포함한다.
# 다만 우리는 messages.create 만 쓰므로, MCP 에이전트 도구/샌드박스 계열은 제외해
# numpy/PIL/uvicorn 같은 무거운 선택 의존성이 따라오지 않게 한다.
_SKIP_ANTHROPIC = ('anthropic.lib.tools', 'anthropic.lib.environments')

hidden = ['pymysql', 'httpx', 'websockets', 'tkinter', 'tkinter.ttk',
          'tkinter.messagebox', 'tkinter.simpledialog',
          'certifi', 'jiter', 'sniffio', 'typing_extensions',
          'annotated_types', 'typing_inspection']
hidden += [m for m in collect_submodules('anthropic') if not m.startswith(_SKIP_ANTHROPIC)]
for pkg in ('pydantic', 'pydantic_core', 'anyio', 'httpx2', 'httpcore2'):
    hidden += collect_submodules(pkg)

datas = collect_data_files('certifi')
# 서버 프로그램 아이콘(창/작업표시줄용) — tools/make_icon.py 로 생성
datas += [('assets/stock_svr.ico', 'assets'), ('assets/stock_svr.png', 'assets')]

a = Analysis(
    ['stock_svr_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'pytest', '_pytest', 'unittest', 'pydoc', 'stock_svr.kiwoom.fake',
        # anthropic 의 MCP/에이전트 도구 계열 선택 의존성 (이 서버는 쓰지 않는다)
        'mcp', 'uvicorn', 'starlette', 'numpy', 'PIL', 'jsonschema',
        'jsonschema_specifications', 'referencing', 'rpds', 'yaml',
        'pydantic_settings', 'python_multipart', 'httpx_sse', 'requests',
        'cryptography', 'setuptools', 'click', 'rich', 'markdown_it', 'mdurl',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(pyz, a.scripts, [], exclude_binaries=True,
              name='stock_svr', console=False, disable_windowed_traceback=False,
              icon='assets/stock_svr.ico')

coll = COLLECT(exe_gui, a.binaries, a.datas,
               strip=False, upx=False, name='stock_svr')
