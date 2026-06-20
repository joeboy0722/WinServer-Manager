import os
import sys
import time
import subprocess
import psutil

# 本小程式的唯一職責是守護主程式，防止其卡死或異常退出
def main():
    if len(sys.argv) < 3:
        # 參數不足，直接退出。格式：python helper_watchdog.py <主程式PID> <啟動命令列表...>
        sys.exit(1)
        
    main_pid = int(sys.argv[1])
    restart_cmd = sys.argv[2:]
    
    last_ping_time = time.time()
    
    # 啟動一個執行緒來讀取 stdin，避免讀取操作阻塞主迴圈的存活偵測
    import threading
    def read_stdin():
        nonlocal last_ping_time
        try:
            for line in sys.stdin:
                line = line.strip()
                if line == "ping":
                    last_ping_time = time.time()
                    # 收到 ping，回覆 pong 並 flush
                    sys.stdout.write("pong\n")
                    sys.stdout.flush()
                elif line == "exit":
                    # 主程式通知安全退出，立即強制結束本守護進程，避免殘留
                    import os
                    os._exit(0)
        except Exception:
            # 管道中斷（可能主程式崩潰），結束本執行緒，交由主執行緒檢測並重啟
            pass

    t = threading.Thread(target=read_stdin, daemon=True)
    t.start()

    while True:
        # 每秒進行存活偵測，但以 30 秒心跳逾時為基準判定主程式卡死
        time.sleep(1)
        
        # 1. 檢查主程式進程是否存在
        if not psutil.pid_exists(main_pid):
            # 主程式已崩潰退出，立刻重啟它
            restart_main_process(main_pid, restart_cmd)
            break
            
        # 2. 檢查心跳是否超時（超過 30 秒沒收到 ping，判定主程式卡死無響應）
        if time.time() - last_ping_time > 30.0:
            # 強制結束主程式 PID，釋放 Port 佔用
            try:
                parent = psutil.Process(main_pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
            except Exception:
                pass
            
            # 重啟主程式
            restart_main_process(main_pid, restart_cmd)
            break

def restart_main_process(old_pid, cmd):
    # 啟動新的主程式前，必須清除 WATCHDOG_STARTED 環境變數
    # 否則新啟動的主程式會繼承它而跳過看門狗的啟動，導致二代主程式失去守護
    env = os.environ.copy()
    if "WATCHDOG_STARTED" in env:
        del env["WATCHDOG_STARTED"]
        
    try:
        # 啟動新的主程式，並在新視窗中啟動以確保獨立執行
        subprocess.Popen(cmd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
    except Exception:
        pass
    # 完成使命後，本守護進程退出（新的主程式會拉起它自己的新守護進程）
    sys.exit(0)

if __name__ == "__main__":
    main()
