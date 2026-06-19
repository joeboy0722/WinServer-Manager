import os
import sys

# 將專案根目錄加入 sys.path，相容以 python backend/main.py 直接啟動之情況
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import zipfile
import asyncio
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum

from backend.config import BASE_DIR, SERVERS_DIR, FRONTEND_DIR
from backend.manager import global_manager

app = FastAPI(title="Windows 伺服器管控系統 API")

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


# --- Pydantic 模型 ---
class ServerCreateReq(BaseModel):
    server_id: str = Field(..., max_length=50)
    name: str = Field(..., max_length=100)

class ServerConfigUpdateReq(BaseModel):
    executable: str = Field(..., max_length=512)
    arguments: Optional[str] = Field("", max_length=2048)
    watchdog_enabled: bool
    ram_limit_mb: int = Field(..., ge=0, le=1048576)  # 限制記憶體在 0MB ~ 1TB 之間

class FileActionReq(BaseModel):
    action: str = Field(..., max_length=20)  # mkdir, delete, rename, write
    path: str = Field(..., max_length=1024)
    new_path: Optional[str] = Field("", max_length=1024)  # rename 時使用
    content: Optional[str] = Field("", max_length=10485760)   # write 時使用，限 10MB


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
            "restart_count": server.restart_count
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
    
    server.save_config_to_disk()
    
    server.append_log(
        f"[系統資訊] 設定已更新: 執行檔={server.executable}, "
        f"參數={server.arguments}, 看門狗={server.watchdog_enabled}, 記憶體限制={server.ram_limit_mb}MB"
    )
    return {"message": "設定修改成功"}

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
async def websocket_endpoint(websocket: WebSocket):
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
    """儲存 Discord 警報全域設定（具備遮罩防覆蓋邏輯）"""
    req_data = req.dict()
    
    # 如果傳入的 token 包含 *，說明前端沒有更改原有的 Token，我們從現有設定中讀取原明文
    if "*" in req_data.get("discord_token", ""):
        old_config = load_global_config()
        req_data["discord_token"] = old_config.get("discord_token", "")
        
    success = save_global_config(req_data)
    if not success:
        raise HTTPException(status_code=500, detail="保存設定失敗")
    return {"message": "設定儲存成功"}

@app.post("/api/global/config/test")
def test_discord_alert(req: GlobalConfigReq):
    """儲存設定並發送 Discord 測試通知（具備遮罩防覆蓋邏輯）"""
    req_data = req.dict()
    
    if "*" in req_data.get("discord_token", ""):
        old_config = load_global_config()
        req_data["discord_token"] = old_config.get("discord_token", "")
        
    save_global_config(req_data)
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


# 掛載前端靜態目錄（最後掛載，以免攔截 API 路由）
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    # 檢測是否處於 PyInstaller 打包環境下
    if getattr(sys, "frozen", False):
        # 打包環境下：停用 reload，並直接傳入 app 物件以防止重新導入模組失敗
        uvicorn.run(app, host="127.0.0.1", port=8000)
    else:
        # 開發環境下：啟用 reload 方便開發偵錯
        uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
