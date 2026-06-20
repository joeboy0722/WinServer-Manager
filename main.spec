import os
from PyInstaller.utils.hooks import collect_all

# 收集 pywin32 的所有二進位檔、資料檔案與隱式匯入，確保 Windows API 呼叫不報錯
datas = []
binaries = []
hiddenimports = []

tmp_ret = collect_all('pywin32')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# 1. 分析與建構主程式 WinServer-Manager.exe
a = Analysis(
    ['backend/main.py'],
    pathex=[os.path.abspath('.')],  # 將專案根目錄加入搜尋路徑，使 PyInstaller 能正確辨識並打包 'backend' 模組
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
    [],
    exclude_binaries=True,  # 不將二進位依賴封裝進單一 exe 中
    name='WinServer-Manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 設為 True 方便除錯
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico',  # 設定 exe 的圖示
)

# 2. 分析與建構看門狗小程式 helper_watchdog.exe
a_watchdog = Analysis(
    ['backend/helper_watchdog.py'],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz_watchdog = PYZ(a_watchdog.pure)

exe_watchdog = EXE(
    pyz_watchdog,
    a_watchdog.scripts,
    [],
    exclude_binaries=True,
    name='helper_watchdog',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 設為 True 方便除錯，主程式會以無視窗模式啟動它
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='helper_logo.ico',
)

# 透過 COLLECT 將兩個 exe、二進位檔與所有資料檔案合併收集到 dist/WinServer-Manager/ 資料夾下
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    exe_watchdog,
    a_watchdog.binaries,
    a_watchdog.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WinServer-Manager',
)
