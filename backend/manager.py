import os
import sys
import time
import shutil
import threading
import subprocess
import collections
import logging
import psutil
import json

# 嘗試自動修復虛擬環境 (venv) 下的 pywin32 DLL 載入路徑
for path in sys.path:
    if "site-packages" in path:
        pywin32_dll_dir = os.path.join(path, "pywin32_system32")
        if os.path.isdir(pywin32_dll_dir):
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(pywin32_dll_dir)
                except Exception:
                    pass
            os.environ["PATH"] = pywin32_dll_dir + os.path.pathsep + os.environ["PATH"]
            break

# 導入 Windows 特有套件（如果有的話，否則動態載入）
try:
    import win32job
    import win32api
    import win32process
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

from backend.config import SERVERS_DIR, MONITOR_INTERVAL, MAX_LOG_LINES

logger = logging.getLogger("server_manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 用於防止重複寫入的全局鎖
_manager_lock = threading.Lock()

class ExistingProcessWrapper:
    """
    包裝一個已在運行的 psutil.Process，使其介面與 subprocess.Popen 相容。
    """
    def __init__(self, psutil_proc):
        self._proc = psutil_proc
        self.pid = psutil_proc.pid
        self.stdout = None  # 無法讀取已運行進程的標準輸出
        self.stdin = None   # 無法寫入已運行進程的標準輸入

    def poll(self):
        """
        模擬 Popen.poll()。
        如果進程仍在運行，返回 None；如果已結束，返回 0。
        """
        try:
            if self._proc.is_running() and self._proc.status() != psutil.STATUS_ZOMBIE:
                return None
            return 0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def wait(self, timeout=None):
        try:
            return self._proc.wait(timeout=timeout)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0
        except psutil.TimeoutExpired:
            raise subprocess.TimeoutExpired(self._proc.cmdline() if hasattr(self._proc, 'cmdline') else '', timeout)

    def kill(self):
        try:
            self._proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def terminate(self):
        try:
            self._proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

class ServerProcess:
    """
    管理單個伺服器進程的啟動、停止、看門狗與日誌
    """
    def __init__(self, server_id: str, name: str):
        self.server_id = server_id
        self.name = name
        self.folder_path = os.path.join(SERVERS_DIR, server_id)
        self.config_file_path = os.path.join(self.folder_path, "config.json")
        
        # 預設設定
        self.executable = ""       # 執行檔相對路徑（例如 run.bat 或 start.exe）
        self.arguments = ""        # 啟動參數
        self.watchdog_enabled = False  # 是否啟用看門狗
        self.ram_limit_mb = 0      # 記憶體限制 (MB)，0 表示不限制
        self.firewall_ports = []   # 防火牆規則列表，例如: [{"port": 80, "protocol": "TCP", "enabled": True, "description": "HTTP"}]
        
        # 執行狀態
        self.process = None        # subprocess.Popen 物件
        self.is_running = False    # 進程是否正在運行
        self.should_be_running = False  # 用戶是否要求該伺服器處於運行狀態
        self.restart_count = 0     # 看門狗重啟次數
        self.last_restart_time = 0.0 # 上次看門狗重啟時間
        
        # 日誌緩衝區
        self.logs = collections.deque(maxlen=MAX_LOG_LINES)
        self.log_thread = None
        self.h_job = None          # Windows Job Object 句柄
        
        # 自動從磁碟載入設定
        self.load_config_from_disk()

        # 偵測並接管已在運行的進程，防止二次啟動
        self.check_and_bind_existing_process()

    def find_running_process(self):
        """
        在系統中尋找是否已有符合此伺服器特徵的進程在運行。
        特徵：
        1. 進程的執行檔路徑 (exe) 剛好是我們的 executable 絕對路徑。
        2. 或者，進程的工作目錄 (cwd) 是我們的 folder_path，且其執行檔路徑在 folder_path 底下 (防止同名不同路徑)。
        3. 或者，如果是 .bat/.cmd 批次檔，進程為 cmd.exe，且其命令列 (cmdline) 包含該執行檔路徑，工作目錄是 folder_path。
        """
        if not self.executable:
            return None

        exec_path = os.path.abspath(os.path.join(self.folder_path, self.executable)).lower()
        folder_path_lower = os.path.abspath(self.folder_path).lower()

        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline', 'cwd']):
            try:
                info = proc.info
                pid = info['pid']
                exe = info['exe']
                cwd = info['cwd']
                cmdline = info['cmdline']

                # 1. 檢查 exe 是否直接匹配
                if exe:
                    exe_abs = os.path.abspath(exe).lower()
                    if exe_abs == exec_path:
                        return proc

                # 2. 檢查工作目錄和執行檔路徑是否在伺服器目錄下 (排除同名不同路徑)
                if cwd and exe:
                    cwd_abs = os.path.abspath(cwd).lower()
                    exe_abs = os.path.abspath(exe).lower()
                    if cwd_abs == folder_path_lower and exe_abs.startswith(folder_path_lower):
                        return proc

                # 3. 檢查 cmd.exe 執行批次檔的情況
                if cmdline and cwd:
                    cwd_abs = os.path.abspath(cwd).lower()
                    if cwd_abs == folder_path_lower:
                        # 檢查命令列中是否包含批次檔檔名或路徑
                        cmdline_str = " ".join(cmdline).lower()
                        if self.executable.lower() in cmdline_str:
                            return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    def check_and_bind_existing_process(self):
        """
        檢查系統中是否已有符合此伺服器特徵的進程在運行。
        若有，則接管該進程，避免重複啟動。
        """
        existing_proc = self.find_running_process()
        if existing_proc:
            self.process = ExistingProcessWrapper(existing_proc)
            self.is_running = True
            self.should_be_running = True
            self.save_config_to_disk()
            self.append_log(f"[系統資訊] 偵測到伺服器進程已在運行中 (PID: {existing_proc.pid})，已自動接管。")
            return True
        return False

    def save_config_to_disk(self):
        """將目前設定持久化儲存到伺服器目錄下的 config.json"""
        data = {
            "name": self.name,
            "executable": self.executable,
            "arguments": self.arguments,
            "watchdog_enabled": self.watchdog_enabled,
            "ram_limit_mb": self.ram_limit_mb,
            "should_be_running": self.should_be_running,  # 儲存伺服器是否應該運行的狀態
            "firewall_ports": self.firewall_ports  # 儲存防火牆設定
        }
        try:
            with open(self.config_file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"儲存伺服器 {self.server_id} 設定檔失敗: {e}")

    def load_config_from_disk(self):
        """從伺服器目錄下的 config.json 載入設定"""
        if os.path.exists(self.config_file_path):
            try:
                with open(self.config_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.name = data.get("name", self.name)
                self.executable = data.get("executable", self.executable)
                self.arguments = data.get("arguments", self.arguments)
                self.watchdog_enabled = data.get("watchdog_enabled", self.watchdog_enabled)
                self.ram_limit_mb = data.get("ram_limit_mb", self.ram_limit_mb)
                self.should_be_running = data.get("should_be_running", self.should_be_running)  # 載入伺服器原本是否應該運行的狀態
                self.firewall_ports = data.get("firewall_ports", [])  # 載入防火牆設定
            except Exception as e:
                logger.error(f"載入伺服器 {self.server_id} 設定檔失敗: {e}")

    def append_log(self, text: str):
        """新增一行日誌，並附上時間戳記"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append(f"[{timestamp}] {text}")

    def _read_output(self):
        """在背景執行緒中讀取進程輸出"""
        try:
            if not self.process or not hasattr(self.process, 'stdout') or self.process.stdout is None:
                self.append_log("[系統資訊] 無法讀取已在運行的外部進程之即時控制台輸出。")
                return

            for raw_line in iter(self.process.stdout.readline, b""):
                if not raw_line:
                    break
                
                # 嘗試使用常見的編碼進行解碼 (繁中cp950, utf-8, 簡中gbk)
                decoded_line = ""
                for encoding in ["utf-8", "cp950", "gbk"]:
                    try:
                        decoded_line = raw_line.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    # 若皆解碼失敗，以 utf-8 並替換錯誤字元解碼，確保不崩潰
                    decoded_line = raw_line.decode("utf-8", errors="replace")
                
                self.append_log(decoded_line.strip())
        except Exception as e:
            self.append_log(f"[系統警告] 讀取進程輸出時發生錯誤: {e}")
        finally:
            self.is_running = False
            self.append_log("[系統資訊] 進程輸出讀取結束。")

    def start(self):
        """啟動伺服器進程"""
        with _manager_lock:
            # 啟動前再次嘗試接管已在運行的進程，防範未偵測到的重複啟動
            if not self.is_running:
                if self.check_and_bind_existing_process():
                    return True, "偵測到進程已在運行，已自動接管"

            if self.is_running:
                return False, "伺服器已在運行中"

            if not self.executable:
                return False, "未指定啟動執行檔"

            exec_path = os.path.join(self.folder_path, self.executable)
            if not os.path.exists(exec_path):
                return False, f"執行檔不存在: {self.executable}"

            # 組合命令與參數
            cmd = f'"{exec_path}"'
            if self.arguments:
                cmd += f" {self.arguments}"

            self.append_log(f"[系統資訊] 正在啟動伺服器... 指令: {cmd}")
            self.append_log(f"[系統資訊] 工作目錄 (CWD): {self.folder_path}")

            try:
                # 啟動進程，將工作目錄設為伺服器自身的資料夾
                # 移除 text=True 並將 bufsize 設為 0 以支援二進位即時無緩衝讀取
                self.process = subprocess.Popen(
                    cmd,
                    cwd=self.folder_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    text=False,
                    bufsize=0,
                    shell=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
                self.is_running = True
                self.should_be_running = True
                self.save_config_to_disk()  # 即時將狀態儲存至磁碟
                
                # 啟動日誌讀取執行緒
                self.log_thread = threading.Thread(target=self._read_output, daemon=True)
                self.log_thread.start()

                # 套用 Windows Job Objects 進行資源限制
                self._apply_job_limits()

                self.append_log(f"[系統資訊] 伺服器啟動成功，PID: {self.process.pid}")
                return True, "啟動成功"
            except Exception as e:
                self.is_running = False
                self.append_log(f"[系統錯誤] 啟動失敗: {e}")
                return False, f"啟動失敗: {e}"

    def write_input(self, command: str) -> bool:
        """向運行中的進程標準輸入 (stdin) 寫入指令"""
        if not self.is_running or not self.process:
            return False
        if not hasattr(self.process, 'stdin') or self.process.stdin is None:
            self.append_log("[系統警告] 無法向已在運行的外部進程發送控制台指令。")
            return False
        try:
            # 取得系統偏好編碼，若無則預設 utf-8
            try:
                import locale
                encoding = locale.getpreferredencoding() or "utf-8"
            except Exception:
                encoding = "utf-8"

            # 將字串指令編碼為 bytes 以寫入二進位 stdin 串流
            try:
                encoded_command = (command + "\n").encode(encoding)
            except Exception:
                encoded_command = (command + "\n").encode("utf-8", errors="replace")

            self.process.stdin.write(encoded_command)
            self.process.stdin.flush()
            self.append_log(f"> [輸入] {command}")
            return True
        except Exception as e:
            self.append_log(f"[系統錯誤] 發送指令失敗: {e}")
            return False

    def _apply_job_limits(self):
        """套用 Windows Job Objects 限制進程資源"""
        if not HAS_WIN32:
            self.append_log("[系統警告] 系統未安裝 pywin32，無法套用 Windows Job Objects 資源限制。")
            return

        # 只有在有記憶體限制時才需要 Job，或者為了確保子進程在關閉時被一併刪除
        try:
            self.h_job = win32job.CreateJobObject(None, "")
            
            # 設定 Job 限制資訊
            # 2000h = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (Job 句柄關閉時，自動終止 Job 內所有進程)
            limit_flags = 0x00002000
            
            ram_limit_bytes = 0
            if self.ram_limit_mb > 0:
                # 100h = JOB_OBJECT_LIMIT_PROCESS_MEMORY (限制單個進程的最大記憶體)
                limit_flags |= 0x00000100
                ram_limit_bytes = self.ram_limit_mb * 1024 * 1024

            limits = win32job.QueryInformationJobObject(self.h_job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] = limit_flags
            if ram_limit_bytes > 0:
                limits['ProcessMemoryLimit'] = ram_limit_bytes

            win32job.SetInformationJobObject(self.h_job, win32job.JobObjectExtendedLimitInformation, limits)

            # 取得子進程的 Process Handle
            h_process = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, 
                False, 
                self.process.pid
            )
            try:
                win32job.AssignProcessToJobObject(self.h_job, h_process)
            finally:
                win32api.CloseHandle(h_process)
            if self.ram_limit_mb > 0:
                self.append_log(f"[系統資訊] 已成功套用 Windows Job Objects 限制記憶體上限為: {self.ram_limit_mb} MB")
            else:
                self.append_log("[系統資訊] 已成功套用 Windows Job Objects 進程樹管理。")
        except Exception as e:
            self.append_log(f"[系統警告] 無法設定 Job Objects 限制 (可能是權限不足或已在其他 Job 中): {e}")
            self.h_job = None

    def stop(self):
        """停止伺服器進程"""
        with _manager_lock:
            self.should_be_running = False
            self.save_config_to_disk()  # 即時將狀態儲存至磁碟
            if not self.is_running or not self.process:
                return False, "伺服器未在運行中"

            self.append_log("[系統資訊] 正在停止伺服器...")
            
            try:
                # 如果有 Job Object，關閉 Job Object 會強制終止其中所有進程
                if self.h_job:
                    # 關閉 Job 句柄，因為設定了 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，會殺死所有子進程
                    self.h_job.close()
                    self.h_job = None
                    self.append_log("[系統資訊] 已透過 Job Object 強制結束所有關聯進程。")
                else:
                    # 遞迴殺死進程樹
                    parent = psutil.Process(self.process.pid)
                    for child in parent.children(recursive=True):
                        child.terminate()
                    parent.terminate()
                    self.append_log("[系統資訊] 已發送停止訊號至進程及其子進程。")
                
                # 等待進程退出
                for _ in range(30):
                    if self.process.poll() is not None:
                        break
                    time.sleep(0.1)
                
                if self.process.poll() is None:
                    # 強制殺死
                    if self.process:
                        self.process.kill()
                    self.append_log("[系統資訊] 進程未響應，已強制殺死 (Kill)。")

                self.is_running = False
                self.append_log("[系統資訊] 伺服器已成功停止。")
                return True, "停止成功"
            except Exception as e:
                self.append_log(f"[系統錯誤] 停止進程時發生錯誤: {e}")
                return False, f"停止失敗: {e}"

    def get_resource_usage(self):
        """取得該伺服器進程的 CPU 與記憶體使用率"""
        if not self.is_running or not self.process:
            return {"cpu": 0.0, "ram": 0.0}

        try:
            parent = psutil.Process(self.process.pid)
            processes = [parent] + parent.children(recursive=True)
            
            total_cpu = 0.0
            total_ram_bytes = 0
            
            for p in processes:
                try:
                    total_cpu += p.cpu_percent(interval=None)
                    total_ram_bytes += p.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            return {
                "cpu": round(total_cpu, 1),
                "ram": round(total_ram_bytes / (1024 * 1024), 1)  # 單位 MB
            }
        except Exception:
            return {"cpu": 0.0, "ram": 0.0}


class ServerManager:
    """
    全域伺服器管理器，監控所有實例，並獲取系統硬體狀態
    """
    def __init__(self):
        self.servers = {}  # server_id -> ServerProcess
        self.monitor_thread = None
        self.is_monitoring = False
        
        # 快取系統硬體歷史數據
        self.system_stats = {
            "cpu": 0.0,
            "ram": 0.0,
            "disk": 0.0,
            "gpu": 0.0,
            "gpu_mem": 0.0
        }
        
        # 載入現有伺服器
        self.reload_servers_from_disk()

    def reload_servers_from_disk(self):
        """掃描 servers 目錄，載入已存在的伺服器"""
        if not os.path.exists(SERVERS_DIR):
            os.makedirs(SERVERS_DIR, exist_ok=True)
            
        for item in os.listdir(SERVERS_DIR):
            item_path = os.path.join(SERVERS_DIR, item)
            if os.path.isdir(item_path):
                # 以資料夾名稱作為 server_id 與伺服器名稱
                if item not in self.servers:
                    self.servers[item] = ServerProcess(item, item)

    def add_server(self, server_id: str, name: str) -> bool:
        """新增一個伺服器實例（建立資料夾並載入）"""
        folder_path = os.path.join(SERVERS_DIR, server_id)
        if os.path.exists(folder_path):
            return False
        
        try:
            os.makedirs(folder_path, exist_ok=True)
            self.servers[server_id] = ServerProcess(server_id, name)
            self.servers[server_id].save_config_to_disk()
            return True
        except Exception as e:
            logger.error(f"建立伺服器資料夾失敗: {e}")
            return False

    def remove_server(self, server_id: str) -> bool:
        """刪除一個伺服器實例（停止進程並刪除資料夾）"""
        if server_id not in self.servers:
            return False
        
        server = self.servers[server_id]
        if server.is_running:
            server.stop()
            
        try:
            # 刪除實體資料夾
            shutil.rmtree(server.folder_path, ignore_errors=True)
            del self.servers[server_id]
            
            # 清理該伺服器的排程任務（動態導入避免循環依賴）(L-2)
            try:
                from backend.scheduler import global_scheduler
                global_scheduler.save_tasks(server_id, [])
                if hasattr(global_scheduler, "_tasks_cache") and server_id in global_scheduler._tasks_cache:
                    del global_scheduler._tasks_cache[server_id]
            except Exception as sched_err:
                logger.error(f"清理伺服器 {server_id} 排程任務失敗: {sched_err}")
                
            # 清理該伺服器的 Windows 防火牆規則
            try:
                from backend.firewall import delete_server_rules
                delete_server_rules(server_id)
            except Exception as fw_err:
                logger.error(f"清理伺服器 {server_id} 防火牆規則失敗: {fw_err}")
                
            return True
        except Exception as e:
            logger.error(f"刪除伺服器資料夾失敗: {e}")
            return False

    def start_monitoring(self):
        """啟動監控與看門狗背景執行緒，並自動恢復原本運行中伺服器實例"""
        self.is_monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # 開機時自動恢復原本應該運行的伺服器
        for server in self.servers.values():
            if server.should_be_running:
                if server.is_running:
                    server.append_log(f"[系統資訊] 偵測到伺服器已在運行中 (PID: {server.process.pid})，已自動接管，不重複啟動。")
                    continue
                server.append_log("[系統資訊] 偵測到此伺服器在系統關閉前處於運行狀態，正在自動恢復運行...")
                # 採用背景執行緒啟動，以防阻塞主監控進程
                threading.Thread(target=server.start, daemon=True).start()

    def stop_monitoring(self):
        """停止監控背景執行緒"""
        self.is_monitoring = False

    def _monitor_loop(self):
        """監控與看門狗的主迴圈"""
        psutil.cpu_percent(interval=None)
        
        while self.is_monitoring:
            try:
                now = time.time()
                
                # 1. 處理看門狗與進程狀態更新
                for server_id, server in list(self.servers.items()):
                    # 檢查進程是否退出
                    if server.process is not None:
                        poll = server.process.poll()
                        if poll is not None:
                            # 進程已結束
                            server.is_running = False
                            
                            # 確保日誌讀取執行緒完全結束，避免競爭導致輸出錯亂或遺漏 (M-3)
                            if server.log_thread and server.log_thread.is_alive():
                                server.log_thread.join(timeout=2.0)
                            
                            # 如果用戶要求該伺服器應該運行，且啟用了看門狗，則觸發重啟
                            if server.should_be_running and server.watchdog_enabled:
                                # 限制重啟頻率（例如冷卻時間 5 秒），避免死循環重啟
                                if now - server.last_restart_time > 5.0:
                                    server.append_log(f"[看門狗警告] 偵測到進程異常退出 (Exit Code: {poll})，即將自動重啟。")
                                    server.last_restart_time = now
                                    server.restart_count += 1
                                    
                                    # 發送 Discord 警報通知
                                    from backend.notifier import send_discord_message_async
                                    send_discord_message_async(
                                        f"⚠️ **[看門狗警報]** 偵測到伺服器 **{server.name} (ID: {server.server_id})** 異常退出 (Exit Code: {poll})，已自動觸發第 {server.restart_count} 次防護重啟！"
                                    )
                                    
                                    # 啟動進程
                                    threading.Thread(target=server.start, daemon=True).start()
                                else:
                                    server.append_log("[看門狗警告] 進程頻繁重啟，暫時進入冷卻保護狀態。")
                            else:
                                if server.should_be_running:
                                    # 沒有啟用看門狗，但意外退出
                                    server.should_be_running = False
                                    server.save_config_to_disk()  # 即時將狀態儲存至磁碟
                                    server.append_log(f"[系統資訊] 進程已退出 (Exit Code: {poll})。")
                                    
                                    # 發送 Discord 警告通知
                                    from backend.notifier import send_discord_message_async
                                    send_discord_message_async(
                                        f"🛑 **[系統警告]** 伺服器 **{server.name} (ID: {server.server_id})** 意外終止，退出代碼: {poll}（看門狗未啟用）。"
                                    )
                
                # 2. 獲取系統硬體指標
                cpu_load = psutil.cpu_percent(interval=None)
                ram_info = psutil.virtual_memory()
                disk_info = psutil.disk_usage('/')
                
                # 3. 獲取 GPU 使用量（優先 NVIDIA GPU）
                gpu_load = 0.0
                gpu_mem_percent = 0.0
                gpu_data = self._get_nvidia_gpu_usage()
                
                if gpu_data:
                    gpu_load = gpu_data[0]["load"]
                    if gpu_data[0]["memory_total"] > 0:
                        gpu_mem_percent = (gpu_data[0]["memory_used"] / gpu_data[0]["memory_total"]) * 100
                
                self.system_stats = {
                    "cpu": round(cpu_load, 1),
                    "ram": round(ram_info.percent, 1),
                    "disk": round(disk_info.percent, 1),
                    "gpu": round(gpu_load, 1),
                    "gpu_mem": round(gpu_mem_percent, 1)
                }
                
            except Exception as e:
                logger.error(f"監控迴圈發生錯誤: {e}")
                
            time.sleep(MONITOR_INTERVAL)

    def _get_nvidia_gpu_usage(self):
        """呼叫 nvidia-smi 獲取 GPU 使用率"""
        if not shutil.which("nvidia-smi"):
            return None
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.total,memory.used", "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=0.5
            )
            if res.returncode == 0:
                lines = res.stdout.strip().split("\n")
                gpu_data = []
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        gpu_data.append({
                            "load": float(parts[0]),
                            "memory_total": float(parts[1]),
                            "memory_used": float(parts[2])
                        })
                return gpu_data
        except Exception:
            pass
        return None

# 全域單例
global_manager = ServerManager()
