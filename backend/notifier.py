import os
import json
import urllib.request
import urllib.parse
import urllib.error
import logging
import threading
import base64
from typing import Tuple

from backend.config import DATA_DIR

logger = logging.getLogger("server_notifier")

# 全局設定檔案路徑
GLOBAL_CONFIG_PATH = os.path.join(DATA_DIR, "global_config.json")

# 嘗試載入 Windows DPAPI 元件 (pywin32)
HAS_WIN32CRYPT = False
try:
    import win32crypt
    HAS_WIN32CRYPT = True
except ImportError:
    logger.warning("[安全提示] 系統未載入 pywin32，Discord Token 將以明文儲存。若要啟用 Windows DPAPI 安全加密，請手動安裝 pywin32。")


def encrypt_token(token: str) -> str:
    """使用 Windows DPAPI 加密 Token"""
    if not token or not token.strip():
        return ""
    if not HAS_WIN32CRYPT:
        return token
    try:
        # 使用 CryptProtectData 加密
        encrypted_data = win32crypt.CryptProtectData(token.encode("utf-8"), None, None, None, None, 0)
        return "dpapi:" + base64.b64encode(encrypted_data).decode("utf-8")
    except Exception as e:
        logger.error(f"DPAPI 加密 Token 失敗，改用明文儲存: {e}")
        return token


def decrypt_token(token: str) -> str:
    """使用 Windows DPAPI 解密 Token"""
    if not token or not token.strip():
        return ""
    if not token.startswith("dpapi:"):
        return token
    if not HAS_WIN32CRYPT:
        logger.error("偵測到 DPAPI 加密 Token，但系統未載入 pywin32，無法解密。")
        return ""
    try:
        raw_b64 = token[6:]
        encrypted_data = base64.b64decode(raw_b64.encode("utf-8"))
        _, decrypted_data = win32crypt.CryptUnprotectData(encrypted_data, None, None, None, 0)
        return decrypted_data.decode("utf-8")
    except Exception as e:
        logger.error(f"DPAPI 解密 Token 失敗: {e}")
        return ""


def load_global_config() -> dict:
    """載入全局設定檔（將自動解密 Token）"""
    config = {
        "discord_enabled": False,
        "discord_token": "",
        "discord_channel_id": "",
        "autostart": True  # 預設啟用開機自啟
    }
    
    if os.path.exists(GLOBAL_CONFIG_PATH):
        try:
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                config.update(data)
        except Exception as e:
            logger.error(f"載入全局設定檔失敗: {e}")
            
    # 解密 Token 以供內部調用
    config["discord_token"] = decrypt_token(config.get("discord_token", ""))
    return config


def save_global_config(config: dict) -> bool:
    """儲存全局設定檔（將自動加密 Token，且具備合併邏輯以防覆蓋其他欄位）"""
    try:
        # 1. 讀取現有的設定檔，保留其他欄位（如密碼雜湊、port 等）
        existing_config = {}
        if os.path.exists(GLOBAL_CONFIG_PATH):
            try:
                with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing_config = json.load(f)
            except Exception as e:
                logger.error(f"讀取現有設定檔失敗，將使用空白設定: {e}")
                
        # 2. 複製一份以防污染傳入的字典
        config_to_save = config.copy()
        
        # 加密 Token 後儲存
        raw_token = config_to_save.get("discord_token", "")
        config_to_save["discord_token"] = encrypt_token(raw_token)
        
        # 3. 將新設定合併至現有設定中
        existing_config.update(config_to_save)
        
        # 4. 寫入設定檔
        with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing_config, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"儲存全局設定檔失敗: {e}")
        return False


def send_discord_message(content: str) -> Tuple[bool, str]:
    """使用標準庫發送訊息到 Discord，具備詳細的錯誤狀態碼處理 (C-3)"""
    config = load_global_config()
    if not config.get("discord_enabled"):
        return False, "Discord 警報未啟用"
        
    token = config.get("discord_token", "").strip()
    channel_id = config.get("discord_channel_id", "").strip()
    
    if not token or not channel_id:
        return False, "Discord Token 或 Channel ID 未設定"

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "WinServerManagerBot (1.0)"
    }
    
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req) as response:
            if response.status in (200, 201):
                return True, "警報訊息發送成功"
            else:
                return False, f"Discord API 回傳未知 HTTP 狀態碼: {response.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Discord Bot Token 無效或已過期 (HTTP 401)"
        elif e.code == 403:
            return False, "Bot 沒有該 Discord 頻道的傳送訊息權限 (HTTP 403)"
        elif e.code == 404:
            return False, "找不到該 Discord Channel ID，請確認頻道是否存在 (HTTP 404)"
        else:
            return False, f"Discord API 傳送失敗: HTTP {e.code} ({e.reason})"
    except Exception as e:
        logger.error(f"Discord 訊息發送異常: {e}")
        return False, f"發送異常: {e}"


def send_discord_message_async(content: str):
    """非同步發送訊息，防止阻塞進程"""
    threading.Thread(target=send_discord_message, args=(content,), daemon=True).start()
