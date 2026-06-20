import os
import sys

# 將專案根目錄加入 sys.path，相容以 python backend/main.py 直接啟動之情況
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import zipfile
import asyncio
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum

from backend.config import BASE_DIR, SERVERS_DIR, FRONTEND_DIR
from backend.manager import global_manager
from backend.auth import hash_password, verify_password, generate_token, check_token, revoke_token
from backend.notifier import load_global_config, save_global_config, send_discord_message


app = FastAPI(title="Windows 伺服器管控系統 API")

@app.on_event("startup")
def startup_event():
    """應用程式啟動時，僅在實際運行的 Worker 進程中啟動背景監控、排程與防火牆檢查服務"""
    import logging
    logger = logging.getLogger("server_manager")
    logger.info("FastAPI startup 事件觸發，正在啟動背景監控、排程與防火牆檢查服務...")
    
    # 1. 啟動伺服器實例監控與看門狗
    global_manager.start_monitoring()
    
    # 2. 啟動定時任務排程器
    global_scheduler.start()
    
    # 3. 啟動 Windows 防火牆每小時安全巡檢對齊背景執行緒
    try:
        from backend.firewall import start_firewall_reconciliation_loop
        start_firewall_reconciliation_loop()
    except Exception as fw_loop_err:
        logger.error(f"啟動防火牆巡檢背景執行緒失敗: {fw_loop_err}")

from fastapi import Request

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """全域安全驗證中間件，自動對受保護 API 進行 Token 驗證"""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        config = load_global_config()
        pw_hash = config.get("password_hash")
        
        # 若尚未設定密碼，阻擋所有非驗證 API
        if not pw_hash:
            return JSONResponse(
                status_code=401,
                content={"detail": "系統尚未設定初始密碼，請先完成初始化設定。"}
            )
            
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "請先登入系統。"}
            )
            
        token = auth_header.split(" ")[1]
        if not check_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "登入認證已過期或無效，請重新登入。"}
            )
            
    response = await call_next(request)
    return response

# 確保前端目錄存在
os.makedirs(FRONTEND_DIR, exist_ok=True)
os.makedirs(os.path.join(FRONTEND_DIR, "css"), exist_ok=True)
os.makedirs(os.path.join(FRONTEND_DIR, "js"), exist_ok=True)


# --- 路徑安全檢查輔助函數 ---
def get_safe_path(server_id: str, relative_path: str) -> str:
    """
    獲取伺服器資料夾底下的安全絕對路徑，防止路徑穿越攻擊 (Path Traversal)
    """
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
        
    server_root = os.path.abspath(server.folder_path)
    
    # 移除傳入路徑頭部的斜線
    clean_rel_path = relative_path.lstrip("/\\")
    target_path = os.path.abspath(os.path.join(server_root, clean_rel_path))
    
    # 確保最終路徑是在伺服器目錄底下
    if not target_path.startswith(server_root):
        raise HTTPException(status_code=403, detail="權限不足：禁止存取伺服器目錄外的檔案")
        
    return target_path


def set_windows_autostart(enabled: bool) -> bool:
    """
    將本程式寫入或移出 Windows 登錄檔的開機啟動清單中。
    """
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "WinServerManager"
    
    # 偵測是否處於 PyInstaller 打包環境下，建構對應的啟動指令
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        # 打包後環境：直接呼叫 exe 檔
        run_cmd = f'"{exe_path}"'
    else:
        python_exe = os.path.abspath(sys.executable)
        script_path = os.path.abspath(sys.argv[0])
        # 開發環境：使用當前 python 執行 main.py
        run_cmd = f'"{python_exe}" "{script_path}"'

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, run_cmd)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                # 若本來就不存在，忽略錯誤
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        # 寫入註冊表失敗時記錄日誌
        import logging
        logging.getLogger("server_notifier").error(f"寫入 Windows 登錄檔開機啟動失敗: {e}")
        return False


def get_watchdog_cmd() -> list:
    """
    獲取啟動 watchdog 守護小程式的命令列表。
    主程式將同時尋找 helper_watchdog.exe 與 helper_watchdog.py。
    """
    import sys
    
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
        internal_dir = getattr(sys, "_MEIPASS", base_dir)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        internal_dir = base_dir

    # 1. 優先尋找同級或解壓目錄下的 helper_watchdog.exe
    exe_paths = [
        os.path.join(base_dir, "helper_watchdog.exe"),
        os.path.join(internal_dir, "helper_watchdog.exe"),
        os.path.join(base_dir, "backend", "helper_watchdog.exe"),
        os.path.join(internal_dir, "backend", "helper_watchdog.exe"),
    ]
    for path in exe_paths:
        if os.path.exists(path):
            return [path]

    # 2. 若找不到 exe，尋找 helper_watchdog.py，並以當前 python 直譯器執行
    py_paths = [
        os.path.join(base_dir, "helper_watchdog.py"),
        os.path.join(base_dir, "backend", "helper_watchdog.py"),
        os.path.join(internal_dir, "helper_watchdog.py"),
        os.path.join(internal_dir, "backend", "helper_watchdog.py"),
    ]
    for path in py_paths:
        if os.path.exists(path):
            return [sys.executable, path]

    default_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper_watchdog.py")
    return [sys.executable, default_py]


# --- Pydantic 模型 ---
import re  # 匯入正規表達式模組，用於防火牆規則名稱解析

class ServerCreateReq(BaseModel):
    server_id: str = Field(..., max_length=50)
    name: str = Field(..., max_length=100)

class FirewallPortItem(BaseModel):
    port: int = Field(..., ge=1, le=65535)
    protocol: str = Field("TCP")
    enabled: bool = True
    description: Optional[str] = Field("", max_length=100)

class ServerConfigUpdateReq(BaseModel):
    executable: str = Field(..., max_length=512)
    arguments: Optional[str] = Field("", max_length=2048)
    watchdog_enabled: bool
    ram_limit_mb: int = Field(..., ge=0, le=1048576)  # 限制記憶體在 0MB ~ 1TB 之間
    firewall_ports: Optional[List[FirewallPortItem]] = []

class FirewallGlobalRuleCreateReq(BaseModel):
    port: int = Field(..., ge=1, le=65535)
    protocol: str = Field("TCP")
    description: Optional[str] = Field("", max_length=100)

class FileActionReq(BaseModel):
    action: str = Field(..., max_length=20)  # mkdir, delete, rename, write
    path: str = Field(..., max_length=1024)
    new_path: Optional[str] = Field("", max_length=1024)  # rename 時使用
    content: Optional[str] = Field("", max_length=10485760)   # write 時使用，限 10MB

class AuthSetupReq(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)

class AuthLoginReq(BaseModel):
    password: str = Field(..., max_length=128)


# --- 認證 API ---

@app.get("/api/auth/status")
def get_auth_status():
    """檢查系統是否需要設定初始密碼"""
    config = load_global_config()
    setup_required = not bool(config.get("password_hash"))
    return {"setup_required": setup_required}

@app.post("/api/auth/setup")
def setup_auth_password(req: AuthSetupReq):
    """設定初始管理密碼"""
    config = load_global_config()
    if config.get("password_hash"):
        raise HTTPException(status_code=403, detail="密碼已設定，禁止重複初始化設定")
        
    pw_hash, salt = hash_password(req.password)
    config["password_hash"] = pw_hash
    config["password_salt"] = salt
    
    success = save_global_config(config)
    if not success:
        raise HTTPException(status_code=500, detail="儲存初始密碼失敗")
        
    # 自動登入並生成 token
    token = generate_token()
    return {"message": "初始密碼設定成功", "token": token}

@app.post("/api/auth/login")
def login_auth(req: AuthLoginReq):
    """管理員密碼驗證登入"""
    config = load_global_config()
    pw_hash = config.get("password_hash")
    salt = config.get("password_salt")
    
    if not pw_hash or not salt:
        raise HTTPException(status_code=400, detail="系統尚未設定初始密碼，請先設定密碼")
        
    if not verify_password(req.password, pw_hash, salt):
        raise HTTPException(status_code=401, detail="密碼錯誤，請重新輸入")
        
    token = generate_token()
    return {"token": token}

@app.post("/api/auth/logout")
def logout_auth(authorization: Optional[str] = Header(None)):
    """註銷當前登入權杖"""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        revoke_token(token)
    return {"message": "登出成功"}


# --- 伺服器管理 API ---

@app.get("/api/servers")
def list_servers():
    """列出所有伺服器與其狀態"""
    # 同步硬體列表
    global_manager.reload_servers_from_disk()
    
    result = []
    for s_id, server in global_manager.servers.items():
        resources = server.get_resource_usage()
        result.append({
            "server_id": server.server_id,
            "name": server.name,
            "is_running": server.is_running,
            "watchdog_enabled": server.watchdog_enabled,
            "ram_limit_mb": server.ram_limit_mb,
            "executable": server.executable,
            "arguments": server.arguments,
            "cpu": resources["cpu"],
            "ram": resources["ram"],
            "restart_count": server.restart_count,
            "firewall_ports": getattr(server, "firewall_ports", [])
        })
    return result

@app.post("/api/servers")
def create_server(req: ServerCreateReq):
    """新增伺服器"""
    # 驗證 ID 格式
    s_id = req.server_id.strip()
    if not s_id or not s_id.isalnum():
        raise HTTPException(status_code=400, detail="伺服器 ID 只能包含英文字母與數字")
        
    success = global_manager.add_server(s_id, req.name)
    if not success:
        raise HTTPException(status_code=400, detail="伺服器已存在或資料夾建立失敗")
        
    return {"message": "伺服器建立成功", "server_id": s_id}

@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str):
    """刪除伺服器"""
    success = global_manager.remove_server(server_id)
    if not success:
        raise HTTPException(status_code=400, detail="伺服器不存在或刪除失敗")
    return {"message": "伺服器已成功刪除"}

@app.get("/api/servers/{server_id}/config")
def get_server_config(server_id: str):
    """取得伺服器啟動設定"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
    return {
        "executable": server.executable,
        "arguments": server.arguments,
        "watchdog_enabled": server.watchdog_enabled,
        "ram_limit_mb": server.ram_limit_mb
    }

@app.post("/api/servers/{server_id}/config")
def update_server_config(server_id: str, req: ServerConfigUpdateReq):
    """修改伺服器啟動設定"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
        
    server.executable = req.executable.strip()
    server.arguments = req.arguments.strip()
    server.watchdog_enabled = req.watchdog_enabled
    server.ram_limit_mb = req.ram_limit_mb
    server.firewall_ports = [item.dict() for item in req.firewall_ports] if req.firewall_ports is not None else []
    
    server.save_config_to_disk()
    
    # 同步 Windows 防火牆規則
    fw_sync_success = True
    fw_sync_msg = ""
    try:
        from backend.firewall import sync_server_firewall_rules
        success, fw_msg = sync_server_firewall_rules(server_id, server.name, server.firewall_ports)
        if not success:
            server.append_log(f"[系統警告] 同步 Windows 防火牆規則失敗: {fw_msg}")
            fw_sync_success = False
            fw_sync_msg = fw_msg
    except Exception as fw_err:
        server.append_log(f"[系統警告] 同步 Windows 防火牆規則出錯: {fw_err}")
        fw_sync_success = False
        fw_sync_msg = str(fw_err)
        
    server.append_log(
        f"[系統資訊] 設定已更新: 執行檔={server.executable}, "
        f"參數={server.arguments}, 看門狗={server.watchdog_enabled}, 記憶體限制={server.ram_limit_mb}MB"
    )
    return {
        "message": "設定修改成功",
        "firewall_sync": {
            "success": fw_sync_success,
            "detail": fw_sync_msg
        }
    }

@app.post("/api/servers/{server_id}/start")
def start_server(server_id: str):
    """啟動伺服器"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
        
    success, msg = server.start()
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}

@app.post("/api/servers/{server_id}/stop")
def stop_server(server_id: str):
    """停止伺服器"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
        
    success, msg = server.stop()
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}

@app.get("/api/servers/{server_id}/logs")
def get_server_logs(server_id: str):
    """取得伺服器控制台最新日誌"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
    return list(server.logs)

@app.delete("/api/servers/{server_id}/logs")
def clear_server_logs(server_id: str):
    """清空伺服器的記憶體日誌緩衝區"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
    server.logs.clear()
    server.append_log("[系統資訊] 控制台日誌已由管理員清空。")
    return {"message": "日誌已成功清空"}


# --- 檔案總管 API ---

@app.get("/api/servers/{server_id}/files")
def list_files(server_id: str, path: str = ""):
    """列出指定相對路徑下的檔案與目錄"""
    abs_path = get_safe_path(server_id, path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="指定路徑不存在")
        
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="指定路徑非目錄")
        
    file_list = []
    try:
        for entry in os.scandir(abs_path):
            stat = entry.stat()
            file_list.append({
                "name": entry.name,
                "path": os.path.relpath(entry.path, global_manager.servers[server_id].folder_path).replace("\\", "/"),
                "is_dir": entry.is_dir(),
                "size": stat.st_size if entry.is_file() else 0,
                "mtime": int(stat.st_mtime)
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取目錄失敗: {e}")
        
    # 目錄排在前面，接著是檔案，皆依名稱排序
    file_list.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return file_list

@app.get("/api/servers/{server_id}/files/read")
def read_file_content(server_id: str, path: str):
    """讀取文字檔案內容"""
    abs_path = get_safe_path(server_id, path)
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="檔案不存在或為目錄")
        
    try:
        # 先嘗試 UTF-8，再試 ANSI/GBK/Big5 等
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(abs_path, "r", encoding="ansi") as f:
                content = f.read()
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取檔案失敗: {e}")

@app.post("/api/servers/{server_id}/files/action")
def file_action(server_id: str, req: FileActionReq):
    """執行檔案操作 (mkdir, delete, rename, write)"""
    abs_path = get_safe_path(server_id, req.path)
    
    if req.action == "mkdir":
        try:
            os.makedirs(abs_path, exist_ok=True)
            return {"message": "目錄建立成功"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"建立目錄失敗: {e}")
            
    elif req.action == "delete":
        if not os.path.exists(abs_path):
            raise HTTPException(status_code=404, detail="目標不存在")
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
            return {"message": "刪除成功"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"刪除失敗: {e}")
            
    elif req.action == "rename":
        if not req.new_path:
            raise HTTPException(status_code=400, detail="請提供新路徑")
        abs_new_path = get_safe_path(server_id, req.new_path)
        if not os.path.exists(abs_path):
            raise HTTPException(status_code=404, detail="原目標不存在")
        if os.path.exists(abs_new_path):
            raise HTTPException(status_code=400, detail="新名稱已存在")
        try:
            os.rename(abs_path, abs_new_path)
            return {"message": "重新命名成功"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"重新命名失敗: {e}")
            
    elif req.action == "write":
        try:
            # 確保上層目錄存在
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(req.content)
            return {"message": "寫入成功"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"寫入檔案失敗: {e}")
            
    else:
        raise HTTPException(status_code=400, detail="無效的操作")

@app.post("/api/servers/{server_id}/files/upload")
async def upload_file(
    server_id: str,
    file: UploadFile = File(...),
    relative_path: str = Form("")
):
    """
    上傳檔案或壓縮檔，如果是壓縮檔則自動解壓
    """
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
        
    # 計算存放檔案的父目錄
    dest_dir = get_safe_path(server_id, relative_path)
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        
    # 組合檔案最終絕對路徑
    target_file_path = os.path.join(dest_dir, file.filename)
    
    # 再次安全檢測防止穿越
    if not os.path.abspath(target_file_path).startswith(os.path.abspath(server.folder_path)):
         raise HTTPException(status_code=403, detail="非法上傳路徑")
         
    try:
        # 分塊寫入檔案
        with open(target_file_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # 1MB chunk
                buffer.write(chunk)
                
        # 判斷是否為壓縮檔 (ZIP)，若是則進行自動解壓
        if file.filename.lower().endswith(".zip"):
            server.append_log(f"[檔案系統] 偵測到壓縮檔上傳，開始解壓縮: {file.filename}")
            try:
                with zipfile.ZipFile(target_file_path, "r") as zip_ref:
                    # 確保解壓出的檔案均沒有 Zip Slip 穿越漏洞
                    for member in zip_ref.namelist():
                        member_path = os.path.abspath(os.path.join(dest_dir, member))
                        if not member_path.startswith(os.path.abspath(server.folder_path)):
                            server.append_log(f"[檔案系統警告] 解壓過程略過非法路徑檔案: {member}")
                            continue
                        zip_ref.extract(member, dest_dir)
                # 刪除 ZIP 檔
                os.remove(target_file_path)
                server.append_log(f"[檔案系統] 壓縮檔解壓完成，已清除原壓縮檔。")
            except Exception as zip_err:
                server.append_log(f"[檔案系統錯誤] 解壓 ZIP 失敗: {zip_err}")
                raise HTTPException(status_code=500, detail=f"ZIP 解壓縮失敗: {zip_err}")
                
        return {"message": "上傳成功", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上傳檔案失敗: {e}")


# --- WebSocket 即時監控傳輸 ---

@app.websocket("/ws/monitor")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None):
    # 進行安全權杖驗證
    config = load_global_config()
    if not config.get("password_hash"):
        await websocket.close(code=1008)
        return
        
    if not token or not check_token(token):
        await websocket.close(code=1008)
        return
        
    await websocket.accept()
    try:
        while True:
            # 整理所有伺服器進程的即時數據
            server_data = {}
            for s_id, server in global_manager.servers.items():
                res = server.get_resource_usage()
                server_data[s_id] = {
                    "is_running": server.is_running,
                    "watchdog_enabled": server.watchdog_enabled,
                    "restart_count": server.restart_count,
                    "cpu": res["cpu"],
                    "ram": res["ram"]
                }
                
            payload = {
                "system": global_manager.system_stats,
                "servers": server_data
            }
            
            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        # 防止 websocket 出錯中斷後端運行
        pass

# --- 擴充 API 模組 ---

import time
from backend.scheduler import global_scheduler
from backend.backup import global_backup_manager, MANIFESTS_DIR
from backend.notifier import load_global_config, save_global_config, send_discord_message

# Pydantic 模型與列舉 (M-1, L-4)
class TaskType(str, Enum):
    restart = "restart"
    backup = "backup"
    command = "command"

class TriggerType(str, Enum):
    time = "time"
    interval = "interval"

class ServerInputReq(BaseModel):
    command: str = Field(..., max_length=2048)

class TaskItem(BaseModel):
    task_id: Optional[str] = None
    name: str = Field(..., max_length=100)
    type: TaskType
    trigger: TriggerType
    value: str = Field(..., max_length=100)
    param: Optional[str] = Field("", max_length=1024)
    enabled: Optional[bool] = True
    last_run: Optional[float] = 0.0

class BackupCreateReq(BaseModel):
    description: str = Field(..., max_length=255)

class GlobalConfigReq(BaseModel):
    discord_enabled: bool
    discord_token: str = Field(..., max_length=255)
    discord_channel_id: str = Field(..., max_length=100)
    autostart: bool

# 1. 控制台指令發送
@app.post("/api/servers/{server_id}/input")
def write_server_input(server_id: str, req: ServerInputReq):
    """向伺服器 stdin 寫入指令"""
    server = global_manager.servers.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="伺服器不存在")
    if not server.is_running:
        raise HTTPException(status_code=400, detail="伺服器未運行，無法發送指令")
    success = server.write_input(req.command)
    if not success:
        raise HTTPException(status_code=500, detail="發送指令失敗")
    return {"message": "指令已成功發送"}

# 2. 定時排程管理
@app.get("/api/servers/{server_id}/scheduler")
def get_scheduler_tasks(server_id: str):
    """列出該伺服器的所有定時任務"""
    return global_scheduler.load_tasks(server_id)

@app.post("/api/servers/{server_id}/scheduler")
def save_scheduler_task(server_id: str, task: TaskItem):
    """新增或修改定時任務"""
    tasks = global_scheduler.load_tasks(server_id)
    
    if not task.task_id:
        # 新增
        task.task_id = f"task_{int(time.time())}"
        tasks.append(task.dict())
    else:
        # 修改
        for i, t in enumerate(tasks):
            if t.get("task_id") == task.task_id:
                tasks[i] = task.dict()
                break
                
    global_scheduler.save_tasks(server_id, tasks)
    return {"message": "定時任務儲存成功", "task_id": task.task_id}

@app.delete("/api/servers/{server_id}/scheduler/{task_id}")
def delete_scheduler_task(server_id: str, task_id: str):
    """刪除定時任務"""
    tasks = global_scheduler.load_tasks(server_id)
    new_tasks = [t for t in tasks if t.get("task_id") != task_id]
    global_scheduler.save_tasks(server_id, new_tasks)
    return {"message": "定時任務刪除成功"}

# 3. 高效去重備份管理
@app.get("/api/servers/{server_id}/backups")
def list_server_backups(server_id: str):
    """取得伺服器歷史備份列表（隱藏敏感的內部檔名）(M-4)"""
    backups = global_backup_manager.list_backups(server_id)
    # 過濾內部使用的 manifest_filename 欄位
    for backup in backups:
        backup.pop("manifest_filename", None)
    return backups

@app.post("/api/servers/{server_id}/backups")
def create_server_backup(server_id: str, req: BackupCreateReq):
    """執行伺服器備份"""
    success, msg = global_backup_manager.create_backup(server_id, req.description)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return {"message": msg}

@app.post("/api/servers/{server_id}/backups/{backup_id}/restore")
def restore_server_backup(server_id: str, backup_id: str):
    """執行伺服器還原"""
    success, msg = global_backup_manager.restore_backup(server_id, backup_id)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return {"message": msg}

@app.delete("/api/servers/{server_id}/backups/{backup_id}")
def delete_server_backup(server_id: str, backup_id: str):
    """刪除備份版本並回收孤兒物件"""
    success, msg = global_backup_manager.delete_backup(server_id, backup_id)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return {"message": msg}

# 4. 全局設定與 Discord API
@app.get("/api/global/config")
def get_global_config_route():
    """載入 Discord 警報等全域設定（傳送給前端時自動實施遮罩）(M-5)"""
    config = load_global_config()
    token = config.get("discord_token", "")
    
    # 進行安全遮罩處理，防止 Token 明文洩漏
    if len(token) > 12:
        config["discord_token"] = token[:6] + "*" * 12 + token[-6:]
    elif token:
        config["discord_token"] = "*" * len(token)
        
    return config

@app.post("/api/global/config")
def save_global_config_route(req: GlobalConfigReq):
    """儲存 Discord 警報與開機自啟等全域設定（具備遮罩防覆蓋邏輯）"""
    req_data = req.dict()
    
    # 如果傳入的 token 包含 *，說明前端沒有更改原有的 Token，我們從現有設定中讀取原明文
    if "*" in req_data.get("discord_token", ""):
        old_config = load_global_config()
        req_data["discord_token"] = old_config.get("discord_token", "")
        
    success = save_global_config(req_data)
    if not success:
        raise HTTPException(status_code=500, detail="保存設定失敗")
        
    # 同步設定 Windows 登錄檔開機自啟
    set_windows_autostart(req_data["autostart"])
    return {"message": "設定儲存成功"}

@app.post("/api/global/config/test")
def test_discord_alert(req: GlobalConfigReq):
    """儲存設定並發送 Discord 測試通知（具備遮罩防覆蓋邏輯）"""
    req_data = req.dict()
    
    if "*" in req_data.get("discord_token", ""):
        old_config = load_global_config()
        req_data["discord_token"] = old_config.get("discord_token", "")
        
    save_global_config(req_data)
    # 同步設定 Windows 登錄檔開機自啟
    set_windows_autostart(req_data["autostart"])
    
    success, msg = send_discord_message("🔌 **[測試通知]** 來自 WinServer Manager 管控系統的 Discord Bot 警報通道測試成功！")
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": "測試訊息已成功發送至您的 Discord 頻道！"}


@app.post("/api/global/backups/cleanup")
def cleanup_orphan_backups():
    """清理已刪除伺服器的殘留備份，並執行垃圾回收"""
    if not os.path.exists(MANIFESTS_DIR):
        return {"message": "備份目錄不存在，無需清理", "deleted_manifests": 0, "deleted_objects": 0}
        
    deleted_manifests_count = 0
    # 遍歷 manifests 目錄下的所有 JSON 備份版本對照表
    for file in os.listdir(MANIFESTS_DIR):
        if file.endswith(".json") and file.startswith("manifest_"):
            # 檔名格式如：manifest_mc01_1718765432.json
            parts = file.split("_")
            if len(parts) >= 3:
                server_id = parts[1]
                # 若該伺服器在當前管理器中已不存在，說明伺服器已遭刪除
                if server_id not in global_manager.servers:
                    file_path = os.path.join(MANIFESTS_DIR, file)
                    try:
                        os.remove(file_path)
                        deleted_manifests_count += 1
                    except Exception as e:
                        # 記錄錯誤日誌
                        pass
                        
    # 執行去重物件庫的垃圾回收 (GC)，清除不再被任何伺服器引用的孤兒備份檔案
    deleted_objects_count = global_backup_manager.garbage_collection()
    
    return {
        "message": f"成功清理了 {deleted_manifests_count} 個已刪除伺服器的備份紀錄，並透過垃圾回收釋放了 {deleted_objects_count} 個無用備份檔案！",
        "deleted_manifests": deleted_manifests_count,
        "deleted_objects": deleted_objects_count
    }


# --- 全域與專案防火牆 API 路由 ---

class ToggleRuleReq(BaseModel):
    enabled: bool

@app.get("/api/firewall/rules")
def get_firewall_rules(all: bool = False):
    """
    取得 Windows 防火牆 Inbound 規則。
    all=True 時查詢系統中所有 Inbound 規則，all=False 時僅查詢 WinServerManager 建立的規則。
    """
    from backend.firewall import list_rules
    try:
        rules = list_rules(all_rules=all)
        return {"rules": rules}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取防火牆規則失敗: {e}")

@app.post("/api/firewall/rules")
def create_global_firewall_rule(req: FirewallGlobalRuleCreateReq):
    """
    新增一條全域防火牆規則 (不綁定特定伺服器)
    """
    from backend.firewall import add_rule
    
    config = load_global_config()
    global_ports = config.get("global_firewall_ports", [])
    
    # 檢查是否已存在
    for p in global_ports:
        if p.get("port") == req.port and p.get("protocol") == req.protocol:
            raise HTTPException(status_code=400, detail="該 Port 與協議已存在於全域規則設定中")
            
    # 新增到設定檔
    new_port = {
        "port": req.port,
        "protocol": req.protocol,
        "enabled": True,
        "description": req.description or ""
    }
    global_ports.append(new_port)
    config["global_firewall_ports"] = global_ports
    save_global_config(config)
    
    # 新增 Windows 防火牆規則
    rule_name = f"WinServerManager_Global_Port_{req.port}_{req.protocol}"
    display_name = f"WinServerManager - Global - {req.port}/{req.protocol}"
    if req.description:
        display_name += f" ({req.description})"
        
    success, msg = add_rule(rule_name, display_name, req.port, req.protocol, enabled=True)
    if not success:
        raise HTTPException(status_code=500, detail=f"全域防火牆規則已儲存，但 Windows 系統規則新增失敗: {msg}")
        
    return {"message": "全域防火牆規則建立成功"}

@app.delete("/api/firewall/rules/{rule_name}")
def delete_firewall_rule(rule_name: str):
    """
    刪除指定的防火牆規則（使用者明確點擊刪除動作）
    """
    from backend.firewall import delete_rule
    
    # 1. 嘗試從 Windows 系統中刪除
    success, msg = delete_rule(rule_name)
    if not success:
        raise HTTPException(status_code=500, detail=f"Windows 系統防火牆規則刪除失敗: {msg}")
    
    # 2. 如果是全域規則，從 global_config.json 移除
    if rule_name.startswith("WinServerManager_Global_"):
        match = re.match(r"WinServerManager_Global_Port_(\d+)_(TCP|UDP)", rule_name)
        if match:
            port = int(match.group(1))
            protocol = match.group(2)
            
            config = load_global_config()
            global_ports = config.get("global_firewall_ports", [])
            new_ports = [p for p in global_ports if not (p.get("port") == port and p.get("protocol") == protocol)]
            config["global_firewall_ports"] = new_ports
            save_global_config(config)
            
    # 3. 如果是伺服器專案規則，從該伺服器的 config.json 移除
    elif rule_name.startswith("WinServerManager_Server_"):
        match = re.match(r"WinServerManager_Server_(.+?)_Port_(\d+)_(TCP|UDP)", rule_name)
        if match:
            server_id = match.group(1)
            port = int(match.group(2))
            protocol = match.group(3)
            
            server = global_manager.servers.get(server_id)
            if server:
                server.firewall_ports = [p for p in server.firewall_ports if not (p.get("port") == port and p.get("protocol") == protocol)]
                server.save_config_to_disk()
                
    return {"message": "防火牆規則已成功刪除"}

@app.put("/api/firewall/rules/{rule_name}/toggle")
def toggle_firewall_rule(rule_name: str, req: ToggleRuleReq):
    """
    啟用/停用指定的防火牆規則
    """
    from backend.firewall import toggle_rule
    
    # 1. 嘗試在 Windows 系統中變更狀態
    success, msg = toggle_rule(rule_name, req.enabled)
    if not success:
        raise HTTPException(status_code=500, detail=f"Windows 系統防火牆規則狀態更新失敗: {msg}")
        
    # 2. 如果是全域規則，同步更新 global_config.json 的狀態
    if rule_name.startswith("WinServerManager_Global_"):
        match = re.match(r"WinServerManager_Global_Port_(\d+)_(TCP|UDP)", rule_name)
        if match:
            port = int(match.group(1))
            protocol = match.group(2)
            
            config = load_global_config()
            global_ports = config.get("global_firewall_ports", [])
            for p in global_ports:
                if p.get("port") == port and p.get("protocol") == protocol:
                    p["enabled"] = req.enabled
                    break
            config["global_firewall_ports"] = global_ports
            save_global_config(config)
            
    # 3. 如果是伺服器專案規則，同步更新伺服器的 config.json 狀態
    elif rule_name.startswith("WinServerManager_Server_"):
        match = re.match(r"WinServerManager_Server_(.+?)_Port_(\d+)_(TCP|UDP)", rule_name)
        if match:
            server_id = match.group(1)
            port = int(match.group(2))
            protocol = match.group(3)
            
            server = global_manager.servers.get(server_id)
            if server:
                for p in server.firewall_ports:
                    if p.get("port") == port and p.get("protocol") == protocol:
                        p["enabled"] = req.enabled
                        break
                server.save_config_to_disk()
                
    return {"message": "防火牆規則狀態更新成功"}


# 掛載前端靜態目錄（最後掛載，以免攔截 API 路由）
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def main_entry():
    import uvicorn
    import argparse
    import subprocess
    import threading
    import atexit
    import time
    
    # 建立參數解析器，用於自訂啟動 Port
    parser = argparse.ArgumentParser(description="Windows 伺服器管控系統")
    parser.add_argument("-port", type=int, default=None, help="指定啟動的連接埠 (Port)")
    
    # 使用 parse_known_args 以避免 uvicorn 在 reload 模式下重啟時因其他參數而報錯
    args, unknown = parser.parse_known_args()
    
    # 載入設定檔
    autostart = True
    config_port = None
    try:
        config = load_global_config()
        config_port = config.get("port")
        autostart = config.get("autostart", True)
    except Exception:
        pass

    # 決定使用的 Port，優先順序：命令列參數 -port > global_config.json 設定檔 > 預設值 8000
    port = args.port
    if port is None and config_port is not None:
        try:
            port = int(config_port)
        except ValueError:
            port = None
            
    if port is None:
        port = 8000
        
    # 自動修復/寫入 Windows 登錄檔開機啟動路徑
    set_windows_autostart(autostart)

    # 啟動與守護小程式的雙向心跳偵測 (加入環境變數守衛，防止 uvicorn reload 模式下重複啟動)
    if os.environ.get("WATCHDOG_STARTED") != "1":
        os.environ["WATCHDOG_STARTED"] = "1"
        watchdog_cmd = get_watchdog_cmd()
        
        # 取得主程式自己的啟動命令 (以便傳給小程式)
        if getattr(sys, "frozen", False):
            # 打包後環境：此時 sys.executable 是 main.exe
            # sys.argv[1:] 包含可能傳入的 -port 等參數
            main_cmd = [sys.executable] + sys.argv[1:]
        else:
            # 開發環境：使用 python.exe 執行 main.py
            # sys.argv 包含 main.py 以及後面的參數
            main_cmd = [sys.executable] + sys.argv
            
        watchdog_process = None
        stop_heartbeat = threading.Event()
        
        def run_watchdog():
            nonlocal watchdog_process
            cmd = watchdog_cmd + [str(os.getpid())] + main_cmd
            try:
                # 啟動守護小程式，重導向其 stdin 與 stdout 進行雙向心跳
                watchdog_process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,  # 使用行緩衝
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            except Exception:
                return

            last_pong_time = time.time()
            
            # 啟動子執行緒讀取小程式的 stdout
            def read_stdout():
                nonlocal last_pong_time
                try:
                    for line in watchdog_process.stdout:
                        if line.strip() == "pong":
                            last_pong_time = time.time()
                except Exception:
                    pass
                    
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stdout_thread.start()
            
            # 主心跳迴圈，每 10 秒發送一次 ping，若 30 秒沒收到 pong 則重啟之
            while not stop_heartbeat.is_set():
                if watchdog_process.poll() is not None:
                    # 小程式已意外退出
                    break
                    
                try:
                    # 發送 ping 心跳
                    watchdog_process.stdin.write("ping\n")
                    watchdog_process.stdin.flush()
                except Exception:
                    break
                    
                # 檢查小程式心跳是否超時 (30 秒)
                if time.time() - last_pong_time > 30.0:
                    try:
                        watchdog_process.kill()
                    except Exception:
                        pass
                    break
                    
                # 等待 10 秒進行下一次心跳
                stop_heartbeat.wait(timeout=10.0)
                
            # 若不是主動停止，代表小程式異常退出或超時，需要在冷卻後重新拉起一個小程式
            if not stop_heartbeat.is_set():
                try:
                    watchdog_process.wait(timeout=2.0)
                except Exception:
                    pass
                time.sleep(2.0)  # 冷卻 2 秒避免死循環
                if not stop_heartbeat.is_set():
                    threading.Thread(target=run_watchdog, daemon=True).start()

        # 啟動守護小程式監控執行緒
        threading.Thread(target=run_watchdog, daemon=True).start()
        
        # 註冊退出掛鉤，在主程式安全關閉前，通知小程式退出並清理進程
        def cleanup_watchdog():
            stop_heartbeat.set()
            if watchdog_process and watchdog_process.poll() is None:
                try:
                    watchdog_process.stdin.write("exit\n")
                    watchdog_process.stdin.flush()
                    watchdog_process.wait(timeout=2.0)
                except Exception:
                    try:
                        watchdog_process.kill()
                    except Exception:
                        pass
                        
        atexit.register(cleanup_watchdog)
        
    # 檢測是否處於 PyInstaller 打包環境下
    if getattr(sys, "frozen", False):
        # 打包環境下：停用 reload，並直接傳入 app 物件以防止重新導入模組失敗
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        # 開發環境下：啟用 reload 方便開發偵錯
        uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main_entry()
