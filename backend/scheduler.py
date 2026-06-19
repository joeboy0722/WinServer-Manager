import os
import json
import time
import datetime
import threading
import logging
from typing import Dict, List, Any

from backend.config import SERVERS_DIR
from backend.manager import global_manager

logger = logging.getLogger("server_scheduler")

class TaskScheduler:
    """
    定時任務排程器，每分鐘檢查一次所有伺服器的 scheduler.json 配置
    """
    def __init__(self):
        self.scheduler_thread = None
        self.is_running = False
        self.lock = threading.Lock()
        self._tasks_cache = {}  # 任務設定快取，key 為 server_id (H-4)

    def get_scheduler_file_path(self, server_id: str) -> str:
        """取得該伺服器的排程任務檔案路徑"""
        return os.path.join(SERVERS_DIR, server_id, "scheduler.json")

    def load_tasks(self, server_id: str) -> List[Dict[str, Any]]:
        """載入特定伺服器的排程任務清單（優先使用記憶體快取避免頻繁 I/O）"""
        if server_id in self._tasks_cache:
            return [task.copy() for task in self._tasks_cache[server_id]]
            
        file_path = self.get_scheduler_file_path(server_id)
        if not os.path.exists(file_path):
            self._tasks_cache[server_id] = []
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
                self._tasks_cache[server_id] = tasks
                return [task.copy() for task in tasks]
        except Exception as e:
            logger.error(f"載入伺服器 {server_id} 定時任務失敗: {e}")
            return []

    def save_tasks(self, server_id: str, tasks: List[Dict[str, Any]]):
        """儲存特定伺服器的排程任務清單，同時刷新快取"""
        self._tasks_cache[server_id] = [task.copy() for task in tasks]
        file_path = self.get_scheduler_file_path(server_id)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"儲存伺服器 {server_id} 定時任務失敗: {e}")

    def start(self):
        """啟動定時任務背景檢查執行緒"""
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self.scheduler_thread.start()
            logger.info("定時任務排程器啟動成功。")

    def stop(self):
        """停止定時任務背景檢查執行緒"""
        with self.lock:
            self.is_running = False

    def _scheduler_loop(self):
        """排程檢測主迴圈，每 30 秒檢查一次"""
        while self.is_running:
            try:
                now = datetime.datetime.now()
                now_str_hm = now.strftime("%H:%M")  # 格式如 "03:00"
                now_timestamp = time.time()
                
                # 遍歷目前所有已載入的伺服器
                for server_id, server in list(global_manager.servers.items()):
                    tasks = self.load_tasks(server_id)
                    tasks_changed = False
                    
                    for task in tasks:
                        if not task.get("enabled", True):
                            continue
                            
                        # 檢查是否達到觸發條件
                        should_trigger = False
                        trigger_type = task.get("trigger")  # "time" 或 "interval"
                        value = task.get("value")          # 定時 "03:00" 或 間隔分鐘數 "360"
                        last_run = task.get("last_run", 0.0)
                        
                        if trigger_type == "time":
                            # 每天定時觸發
                            # 檢查時間是否相符，且今天尚未執行過 (同一個分鐘內只執行一次，故以天為界或隔天重設)
                            if now_str_hm == value:
                                # 如果上次執行時間距離現在大於 60 秒，且不是今天同一分鐘
                                last_run_dt = datetime.datetime.fromtimestamp(last_run) if last_run > 0 else None
                                if not last_run_dt or last_run_dt.date() < now.date():
                                    should_trigger = True
                                    
                        elif trigger_type == "interval":
                            # 間隔分鐘數觸發
                            interval_seconds = int(value) * 60
                            if now_timestamp - last_run >= interval_seconds:
                                # 如果是初次運行，將其 last_run 初始化為目前時間，防止啟動時全部瞬間執行
                                if last_run == 0.0:
                                    task["last_run"] = now_timestamp
                                    tasks_changed = True
                                else:
                                    should_trigger = True
                        
                        if should_trigger:
                            # 更新任務上次執行時間
                            task["last_run"] = now_timestamp
                            tasks_changed = True
                            
                            # 在獨立執行緒中執行任務，避免阻塞 Scheduler 迴圈
                            threading.Thread(
                                target=self._execute_task,
                                args=(server_id, task.copy()),
                                daemon=True
                            ).start()
                            
                    if tasks_changed:
                        self.save_tasks(server_id, tasks)
                        
            except Exception as e:
                logger.error(f"定時任務檢測迴圈發生錯誤: {e}")
                
            time.sleep(30)

    def _execute_task(self, server_id: str, task: Dict[str, Any]):
        """執行特定排程任務"""
        server = global_manager.servers.get(server_id)
        if not server:
            return

        task_type = task.get("type")  # "restart", "backup", "command"
        task_name = task.get("name", "未命名任務")
        param = task.get("param", "")

        server.append_log(f"[定時任務] 開始執行自動排程任務: {task_name} (類型: {task_type})")
        
        try:
            if task_type == "restart":
                # 執行重啟 (僅在伺服器原本就在運行時執行重啟，避免手動關閉後被定時任務重新開啟)
                if server.is_running:
                    server.stop()
                    # 等待一小段時間讓進程完全結束
                    time.sleep(2)
                    server.start()
                    server.append_log(f"[定時任務] 自動重啟任務完成。")
                else:
                    server.append_log(f"[定時任務警告] 伺服器當前未運行，略過自動重啟任務。")
                
            elif task_type == "backup":
                # 執行備份 (導入備份模組以避免循環引入)
                from backend.backup import global_backup_manager
                success, msg = global_backup_manager.create_backup(server_id, f"自動定時備份: {task_name}")
                if success:
                    server.append_log(f"[定時任務] 自動備份任務成功: {msg}")
                else:
                    server.append_log(f"[定時任務錯誤] 自動備份任務失敗: {msg}")
                    
            elif task_type == "command":
                # 傳送指令
                if server.is_running:
                    success = server.write_input(param)
                    if success:
                        server.append_log(f"[定時任務] 已成功發送排程指令: {param}")
                    else:
                        server.append_log(f"[定時任務錯誤] 發送排程指令失敗: {param}")
                else:
                    server.append_log(f"[定時任務警告] 伺服器未運行，忽略排程指令: {param}")
                    
        except Exception as e:
            server.append_log(f"[定時任務錯誤] 執行任務時發生異常: {e}")
            logger.error(f"執行排程任務 {task_name} 失敗: {e}")

# 全域單例排程器
global_scheduler = TaskScheduler()
global_scheduler.start()
