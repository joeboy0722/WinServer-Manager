import os
import sys

# 專案根目錄 (在 PyInstaller 打包下 sys._MEIPASS 是解壓目錄)
if getattr(sys, 'frozen', False):
    # 打包後的臨時資源根目錄 (唯讀資源)
    BASE_DIR = os.path.join(sys._MEIPASS, "backend")
    # 執行檔所在的實體目錄 (用於存放用戶伺服器、備份與設定檔，保證不因臨時目錄清理而丟失)
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
    # 前端靜態檔案目錄 (用戶要求另外放置，與生成的 exe 檔案同級)
    FRONTEND_DIR = os.path.join(DATA_DIR, "frontend")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR
    FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))

# 伺服器實例資料夾的根目錄
SERVERS_DIR = os.path.join(DATA_DIR, "servers")

# 確保伺服器根目錄存在
os.makedirs(SERVERS_DIR, exist_ok=True)

# 硬體監控與看門狗輪詢間隔（秒）
MONITOR_INTERVAL = 1.0

# 每個伺服器記憶體中保留的最新日誌最大行數
MAX_LOG_LINES = 1000
