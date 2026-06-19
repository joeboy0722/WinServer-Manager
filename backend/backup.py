import os
import shutil
import hashlib
import json
import time
import logging
from typing import Dict, List, Tuple, Set, Any

from backend.config import DATA_DIR, SERVERS_DIR

logger = logging.getLogger("server_backup")

# 備份根目錄與子目錄
BACKUP_ROOT_DIR = os.path.abspath(os.path.join(DATA_DIR, "backups"))
OBJECTS_DIR = os.path.join(BACKUP_ROOT_DIR, "objects")
MANIFESTS_DIR = os.path.join(BACKUP_ROOT_DIR, "manifests")

# 確保目錄存在
os.makedirs(OBJECTS_DIR, exist_ok=True)
os.makedirs(MANIFESTS_DIR, exist_ok=True)


def get_file_sha256(file_path: str) -> str:
    """分塊讀取檔案並計算 SHA-256 雜湊值"""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(128 * 1024):  # 128KB chunk
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        logger.error(f"計算檔案雜湊失敗: {file_path}, 錯誤: {e}")
        return ""


class ContentAddressableBackupManager:
    """
    Git 內容定址去重備份管理器
    """
    def __init__(self):
        pass

    def get_object_path(self, sha256: str) -> Tuple[str, str]:
        """
        根據 SHA-256 雜湊值計算其在物件庫中的路徑
        取前 2 個字元作為子資料夾名稱，剩下的 62 個字元作為檔名
        回傳: (子目錄路徑, 完整檔案路徑)
        """
        sub_dir = os.path.join(OBJECTS_DIR, sha256[:2])
        file_path = os.path.join(sub_dir, sha256[2:])
        return sub_dir, file_path

    def create_backup(self, server_id: str, description: str) -> Tuple[bool, str]:
        """建立伺服器備份"""
        server_root = os.path.abspath(os.path.join(SERVERS_DIR, server_id))
        if not os.path.exists(server_root):
            return False, "伺服器目錄不存在"

        timestamp = int(time.time())
        backup_id = f"backup_{server_id}_{time.strftime('%Y%m%d_%H%M%S')}"
        
        manifest = {
            "backup_id": backup_id,
            "server_id": server_id,
            "timestamp": timestamp,
            "description": description,
            "files": []
        }

        # 系統保留檔案清單（不備份也不還原）
        system_files = {"config.json", "scheduler.json"}

        try:
            # 遍歷伺服器目錄下所有檔案
            for root, dirs, files in os.walk(server_root):
                for file in files:
                    # 略過系統保留檔案與臨時 Lock 檔
                    if file in system_files or file.endswith(".lock"):
                        continue
                        
                    abs_file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_file_path, server_root).replace("\\", "/")
                    
                    # 計算 SHA-256 雜湊
                    sha256 = get_file_sha256(abs_file_path)
                    if not sha256:
                        continue  # 檔案讀取失敗則跳過
                        
                    file_size = os.path.getsize(abs_file_path)
                    file_mtime = int(os.path.getmtime(abs_file_path))
                    
                    # 檢查並存入全局物件庫 (Objects Store)
                    sub_dir, obj_path = self.get_object_path(sha256)
                    if not os.path.exists(obj_path):
                        os.makedirs(sub_dir, exist_ok=True)
                        shutil.copy2(abs_file_path, obj_path)  # 複製並保留修改時間
                        
                    # 記錄在 Manifest 中
                    manifest["files"].append({
                        "rel_path": rel_path,
                        "size": file_size,
                        "mtime": file_mtime,
                        "sha256": sha256
                    })
            
            # 寫入 Manifest JSON 檔案
            manifest_file = os.path.join(MANIFESTS_DIR, f"manifest_{server_id}_{timestamp}.json")
            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=4)
                
            return True, f"備份成功，ID: {backup_id}"
            
        except Exception as e:
            logger.error(f"建立伺服器 {server_id} 備份失敗: {e}")
            return False, f"備份失敗: {e}"

    def list_backups(self, server_id: str) -> List[Dict[str, Any]]:
        """列出特定伺服器的所有歷史備份版本"""
        backup_list = []
        if not os.path.exists(MANIFESTS_DIR):
            return []
            
        for file in os.listdir(MANIFESTS_DIR):
            # 格式：manifest_{server_id}_{timestamp}.json
            if file.startswith(f"manifest_{server_id}_") and file.endswith(".json"):
                file_path = os.path.join(MANIFESTS_DIR, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        
                    # 計算此備份版本的總大小 (去重前的大小)
                    total_size = sum(f.get("size", 0) for f in data.get("files", []))
                    file_count = len(data.get("files", []))
                    
                    backup_list.append({
                        "backup_id": data.get("backup_id"),
                        "server_id": data.get("server_id"),
                        "timestamp": data.get("timestamp"),
                        "description": data.get("description"),
                        "total_size": total_size,
                        "file_count": file_count,
                        "manifest_filename": file
                    })
                except Exception as e:
                    logger.error(f"解析備份檔 {file} 失敗: {e}")
                    
        # 依時間戳記由新到舊排序
        backup_list.sort(key=lambda x: x["timestamp"], reverse=True)
        return backup_list

    def restore_backup(self, server_id: str, backup_id: str) -> Tuple[bool, str]:
        """將伺服器還原至特定的備份版本（具備原子性與路徑安全保護）"""
        server_root = os.path.abspath(os.path.join(SERVERS_DIR, server_id))
        if not os.path.exists(server_root):
            return False, "伺服器目錄不存在"
            
        # 尋找對應的 manifest 檔案
        manifest_data = None
        manifest_file_path = ""
        
        for file in os.listdir(MANIFESTS_DIR):
            if file.startswith(f"manifest_{server_id}_") and file.endswith(".json"):
                path = os.path.join(MANIFESTS_DIR, file)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("backup_id") == backup_id:
                        manifest_data = data
                        manifest_file_path = path
                        break
                except Exception:
                    pass
                    
        if not manifest_data:
            return False, "找不到指定的備份版本"

        system_files = {"config.json", "scheduler.json"}
        
        # 定義還原的臨時目錄與舊檔案備份目錄
        temp_restore_root = os.path.abspath(os.path.join(SERVERS_DIR, f"{server_id}_restore_temp"))
        temp_old_root = os.path.abspath(os.path.join(SERVERS_DIR, f"{server_id}_old_temp"))
        
        # 確保清理先前的殘留目錄
        if os.path.exists(temp_restore_root):
            shutil.rmtree(temp_restore_root)
        if os.path.exists(temp_old_root):
            shutil.rmtree(temp_old_root)
            
        os.makedirs(temp_restore_root, exist_ok=True)

        try:
            # 1. 根據 Manifest 將所有檔案還原至臨時目錄，並進行路徑安全檢查 (C-1)
            for file_info in manifest_data.get("files", []):
                rel_path = file_info.get("rel_path")
                sha256 = file_info.get("sha256")
                
                # 計算目標路徑（暫存目錄）
                temp_dest_file_path = os.path.abspath(os.path.join(temp_restore_root, rel_path))
                _, obj_path = self.get_object_path(sha256)
                
                # 預防路徑穿越 (C-1)
                if not temp_dest_file_path.startswith(temp_restore_root + os.sep):
                    logger.warning(f"略過非法還原路徑，防止路徑穿越: {rel_path}")
                    continue
                
                if not os.path.exists(obj_path):
                    raise FileNotFoundError(f"物件庫遺失檔案 {rel_path} (SHA: {sha256})")
                    
                # 確保父目錄存在
                os.makedirs(os.path.dirname(temp_dest_file_path), exist_ok=True)
                
                # 複製檔案至臨時目錄
                shutil.copy2(obj_path, temp_dest_file_path)
                
            # 2. 檔案還原成功後，實施兩階段替換，確保原子性 (H-5)
            os.makedirs(temp_old_root, exist_ok=True)
            
            # 將原本伺服器目錄下的非系統保留檔案，移至臨時舊檔案目錄中
            for item in os.listdir(server_root):
                if item in system_files:
                    continue
                item_path = os.path.join(server_root, item)
                dest_path = os.path.join(temp_old_root, item)
                shutil.move(item_path, dest_path)
                
            # 將還原好的新檔案，由臨時還原目錄移入原伺服器目錄
            for item in os.listdir(temp_restore_root):
                item_path = os.path.join(temp_restore_root, item)
                dest_path = os.path.join(server_root, item)
                shutil.move(item_path, dest_path)
                
            # 還原成功，清理臨時資料夾
            shutil.rmtree(temp_restore_root)
            shutil.rmtree(temp_old_root)
            return True, "還原成功"
            
        except Exception as e:
            logger.error(f"還原伺服器 {server_id} 備份失敗: {e}")
            # 3. 異常復原機制：若中途發生異常，將移出的舊檔案移回 server_root (H-5)
            try:
                if os.path.exists(temp_old_root):
                    for item in os.listdir(temp_old_root):
                        item_path = os.path.join(temp_old_root, item)
                        dest_path = os.path.join(server_root, item)
                        if os.path.exists(dest_path):
                            if os.path.isdir(dest_path):
                                shutil.rmtree(dest_path)
                            else:
                                os.remove(dest_path)
                        shutil.move(item_path, dest_path)
            except Exception as rollback_err:
                logger.error(f"還原失敗後的回滾操作失敗: {rollback_err}")
                
            # 清理臨時還原目錄與備份目錄
            try:
                if os.path.exists(temp_restore_root):
                    shutil.rmtree(temp_restore_root)
                if os.path.exists(temp_old_root):
                    shutil.rmtree(temp_old_root)
            except Exception:
                pass
                
            return False, f"還原失敗: {e}"

    def delete_backup(self, server_id: str, backup_id: str) -> Tuple[bool, str]:
        """刪除指定的備份版本，並自動執行垃圾回收 (GC)"""
        manifest_file_path = ""
        for file in os.listdir(MANIFESTS_DIR):
            if file.startswith(f"manifest_{server_id}_") and file.endswith(".json"):
                path = os.path.join(MANIFESTS_DIR, file)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("backup_id") == backup_id:
                        manifest_file_path = path
                        break
                except Exception:
                    pass
                    
        if not manifest_file_path:
            return False, "找不到指定的備份檔案"

        try:
            # 1. 刪除 Manifest JSON
            os.remove(manifest_file_path)
            
            # 2. 執行垃圾回收以清除無用物件
            deleted_objects_count = self.garbage_collection()
            
            return True, f"刪除成功，並回收了 {deleted_objects_count} 個無用檔案。"
        except Exception as e:
            logger.error(f"刪除備份版本失敗: {e}")
            return False, f"刪除失敗: {e}"

    def garbage_collection(self) -> int:
        """
        執行垃圾回收 (GC)。
        比對目前 manifests/ 下所有版本所引用的 SHA-256。
        若 objects/ 底下有些物件不再被任何版本引用，則將其刪除。
        """
        referenced_shas: Set[str] = set()
        
        # 1. 蒐集所有 Manifest 中引用的 SHA-256 雜湊
        for file in os.listdir(MANIFESTS_DIR):
            if file.endswith(".json"):
                path = os.path.join(MANIFESTS_DIR, file)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for file_info in data.get("files", []):
                        referenced_shas.add(file_info.get("sha256"))
                except Exception:
                    pass

        deleted_count = 0
        
        # 2. 遍歷 objects 目錄下的兩層子資料夾
        if not os.path.exists(OBJECTS_DIR):
            return 0
            
        for first_two in os.listdir(OBJECTS_DIR):
            sub_dir = os.path.join(OBJECTS_DIR, first_two)
            if not os.path.isdir(sub_dir):
                continue
                
            for remaining in os.listdir(sub_dir):
                sha256 = first_two + remaining
                obj_file_path = os.path.join(sub_dir, remaining)
                
                # 若物件不再被任何備份版本引用，將其刪除
                if sha256 not in referenced_shas:
                    try:
                        os.remove(obj_file_path)
                        deleted_count += 1
                    except Exception:
                        pass
                        
            # 如果子資料夾空了，順便把子資料夾也刪了
            try:
                if not os.listdir(sub_dir):
                    os.rmdir(sub_dir)
            except Exception:
                pass
                
        return deleted_count

# 全域單例
global_backup_manager = ContentAddressableBackupManager()
