# -*- coding: utf-8 -*-
"""
Windows 防火牆管理模組
"""
import os
import re
import json
import time
import ctypes
import logging
import threading
import subprocess
from typing import List, Dict, Tuple, Any

from backend.notifier import load_global_config, save_global_config

logger = logging.getLogger("server_firewall")

def execute_cmd(cmd: List[str]) -> Tuple[bool, str, str]:
    """
    執行 Windows 系統指令，並以合適的編碼解碼輸出
    """
    try:
        # 使用 DETACHED_PROCESS (0x00000008) 代替 CREATE_NO_WINDOW，並明確重導向 stdin=subprocess.DEVNULL。
        # 確保在 Windows Session 0背景服務下呼叫防火牆指令時，進程不會因為控制台初始化出錯而發生 0xC0000142 閃退。
        creation_flags = 0x00000008 if os.name == 'nt' else 0
        res = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=15,
            creationflags=creation_flags
        )
        
        # 嘗試以常用 Windows 與通用編碼解碼
        stdout = ""
        stderr = ""
        for encoding in ["utf-8", "cp950", "gbk"]:
            try:
                stdout = res.stdout.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            stdout = res.stdout.decode("utf-8", errors="replace")
            
        for encoding in ["utf-8", "cp950", "gbk"]:
            try:
                stderr = res.stderr.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            stderr = res.stderr.decode("utf-8", errors="replace")
            
        return res.returncode == 0, stdout, stderr
    except Exception as e:
        return False, "", str(e)


def is_admin() -> bool:
    """
    檢查目前程式是否具有管理員權限
    """
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def list_rules(all_rules: bool = False) -> List[Dict[str, Any]]:
    """
    獲取系統中的 Windows 防火牆 Inbound 規則。
    如果 all_rules=False，僅查詢本系統建立的規則 (以 'WinServerManager_*' 開頭)。
    """
    # 建立 PowerShell 查詢指令，使用 pipeline Where-Object 過濾 DisplayName 或 Name，
    # 避免因為 Windows netsh 建立規則時 Name 被設為 GUID 而過濾失敗
    filter_pipe = "" if all_rules else "| Where-Object { $_.DisplayName -like 'WinServerManager_*' -or $_.Name -like 'WinServerManager_*' }"
    
    # 透過 PowerShell 查詢防火牆規則並將其轉換成 JSON 字串
    ps_cmd = (
        f"Get-NetFirewallRule -Direction Inbound -ErrorAction SilentlyContinue {filter_pipe} | "
        "ForEach-Object { "
        "  $r = $_; "
        "  $f = $r | Get-NetFirewallPortFilter; "
        "  [PSCustomObject]@{ "
        "    Name = $r.Name; "
        "    DisplayName = $r.DisplayName; "
        "    Enabled = $r.Enabled.ToString(); "
        "    Action = $r.Action.ToString(); "
        "    Protocol = $f.Protocol.ToString(); "
        "    LocalPort = $f.LocalPort.ToString() "
        "  } "
        "} | ConvertTo-Json -Compress"
    )
    
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd]
    success, stdout, stderr = execute_cmd(cmd)
    
    if not success or not stdout.strip():
        return []
        
    try:
        data = json.loads(stdout.strip())
        # 如果只有單個規則，PowerShell 轉 JSON 會是物件形式，包裝成陣列
        if isinstance(data, dict):
            data = [data]
            
        rules = []
        for item in data:
            # 優先使用 DisplayName，因為 netsh 建立規則時，name 參數設定的是 DisplayName，
            # 而內部的 Name (ID) 在某些 Windows 系統上會被設為隨機 GUID。
            # 使用 DisplayName 可以確保與 netsh delete/toggle 的 name 參數完美一致。
            name = item.get("DisplayName", "") or item.get("Name", "")
            
            # 從規則名稱解析 server_id
            # 格式：WinServerManager_Server_{server_id}_Port_{port}_{protocol}
            # 或：WinServerManager_Global_Port_{port}_{protocol}
            server_id = None
            is_global = False
            
            if name.startswith("WinServerManager_Server_"):
                match = re.match(r"WinServerManager_Server_(.+?)_Port_", name)
                if match:
                    server_id = match.group(1)
            elif name.startswith("WinServerManager_Global_"):
                is_global = True
                
            rules.append({
                "name": name,
                "display_name": item.get("DisplayName", ""),
                "enabled": item.get("Enabled", "False") == "True",
                "action": item.get("Action", "Allow"),
                "protocol": item.get("Protocol", "TCP"),
                "local_port": item.get("LocalPort", ""),
                "server_id": server_id,
                "is_global": is_global
            })
        return rules
    except Exception as e:
        logger.error(f"解析防火牆規則失敗: {e}, 輸出內容: {stdout}")
        return []


def add_rule(name: str, display_name: str, port: int, protocol: str = "TCP", enabled: bool = True) -> Tuple[bool, str]:
    """
    新增一條 Windows 防火牆 Inbound 允許規則。
    優先使用 netsh，速度快且穩定。
    """
    enable_str = "yes" if enabled else "no"
    cmd = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={name}",
        "dir=in",
        "action=allow",
        f"protocol={protocol}",
        f"localport={port}",
        f"enable={enable_str}"
    ]
    success, stdout, stderr = execute_cmd(cmd)
    if not success:
        error_msg = stdout.strip() or stderr.strip() or "netsh 執行失敗且無輸出"
        logger.error(f"建立防火牆規則失敗: {error_msg}")
        return False, error_msg
    return True, "新增成功"


def delete_rule(name: str) -> Tuple[bool, str]:
    """
    刪除指定的 Windows 防火牆規則
    """
    cmd = ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"]
    success, stdout, stderr = execute_cmd(cmd)
    if not success:
        error_msg = stdout.strip() or stderr.strip() or "netsh 執行失敗且無輸出"
        logger.error(f"刪除防火牆規則失敗: {error_msg}")
        return False, error_msg
    return True, "刪除成功"


def toggle_rule(name: str, enabled: bool) -> Tuple[bool, str]:
    """
    啟用或停用指定的 Windows 防火牆規則
    """
    enable_str = "yes" if enabled else "no"
    cmd = [
        "netsh", "advfirewall", "firewall", "set", "rule",
        f"name={name}",
        "new",
        f"enable={enable_str}"
    ]
    success, stdout, stderr = execute_cmd(cmd)
    if not success:
        error_msg = stdout.strip() or stderr.strip() or "netsh 執行失敗且無輸出"
        logger.error(f"修改防火牆規則狀態失敗: {error_msg}")
        return False, error_msg
    return True, "更新狀態成功"


def delete_server_rules(server_id: str) -> Tuple[bool, str]:
    """
    刪除特定伺服器實例的所有防火牆規則。
    使用 PowerShell 支援萬用字元一鍵清除。
    """
    ps_cmd = f"Remove-NetFirewallRule -DisplayName 'WinServerManager_Server_{server_id}_*' -ErrorAction SilentlyContinue"
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd]
    success, stdout, stderr = execute_cmd(cmd)
    if not success:
        error_msg = stdout.strip() or stderr.strip() or "PowerShell 執行失敗且無輸出"
        logger.error(f"清除伺服器 {server_id} 防火牆規則失敗: {error_msg}")
        return False, error_msg
    return True, "清除伺服器防火牆規則成功"


def sync_server_firewall_rules(server_id: str, server_name: str, ports: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    同步伺服器的 Port 設定到系統防火牆中。
    1. 獲取系統中當前屬於該伺服器的防火牆規則。
    2. 比對使用者設定的 `ports`。
    3. 新增缺少的，修改狀態不一致的，刪除設定中已被移除的（因為此處是使用者明確點擊儲存同步的操作）。
    """
    try:
        system_rules = list_rules(all_rules=False)
        server_rules = [r for r in system_rules if r["server_id"] == server_id]
        
        system_rules_dict = {
            f"{r['local_port']}_{r['protocol']}": r for r in server_rules
        }
        
        target_rules_dict = {
            f"{p['port']}_{p['protocol']}": p for p in ports
        }
        
        failed_rules = []
        
        # 刪除已被移除的規則
        for key, sys_rule in system_rules_dict.items():
            if key not in target_rules_dict:
                success, err_msg = delete_rule(sys_rule["name"])
                if not success:
                    failed_rules.append(f"刪除規則 {sys_rule['name']} 失敗: {err_msg}")
                
        # 新增或修改規則
        for key, target in target_rules_dict.items():
            port = target["port"]
            protocol = target["protocol"]
            enabled = target["enabled"]
            desc = target.get("description", "")
            
            rule_name = f"WinServerManager_Server_{server_id}_Port_{port}_{protocol}"
            display_name = f"WinServerManager - {server_name} - {port}/{protocol}"
            if desc:
                display_name += f" ({desc})"
                
            if key in system_rules_dict:
                sys_rule = system_rules_dict[key]
                if sys_rule["enabled"] != enabled:
                    success, err_msg = toggle_rule(rule_name, enabled)
                    if not success:
                        failed_rules.append(f"變更規則 {rule_name} 狀態失敗: {err_msg}")
            else:
                # 補建規則
                success, err_msg = add_rule(rule_name, display_name, port, protocol, enabled=enabled)
                if not success:
                    failed_rules.append(f"建立規則 {rule_name} 失敗: {err_msg}")
                
        if failed_rules:
            error_detail = "; ".join(failed_rules)
            return False, error_detail
            
        return True, "同步成功"
    except Exception as e:
        logger.error(f"同步伺服器 {server_id} 防火牆規則失敗: {e}")
        return False, str(e)


def reconcile_firewall_rules():
    """
    安全對齊檢查機制（只增不減，補回遺失規則，不主動刪除多餘的防火牆規則）
    1. 收集所有伺服器設定中的啟用規則
    2. 收集全局設定中的啟用規則
    3. 比對系統中的規則，若有缺失則自動補回
    """
    from backend.manager import global_manager
    
    logger.info("[防火牆對齊] 開始執行防火牆對齊檢查...")
    
    # 1. 收集所有伺服器的 Port 設定
    expected_rules = {}  # 規則名稱 -> (顯示名稱, port, protocol, enabled)
    
    for server_id, server in list(global_manager.servers.items()):
        ports = getattr(server, "firewall_ports", [])
        for p in ports:
            if not p.get("enabled", True):
                continue
            port = p["port"]
            protocol = p["protocol"]
            desc = p.get("description", "")
            
            rule_name = f"WinServerManager_Server_{server_id}_Port_{port}_{protocol}"
            display_name = f"WinServerManager - {server.name} - {port}/{protocol}"
            if desc:
                display_name += f" ({desc})"
                
            expected_rules[rule_name] = (display_name, port, protocol)
            
    # 2. 收集全域 Port 設定
    g_config = load_global_config()
    global_ports = g_config.get("global_firewall_ports", [])
    for p in global_ports:
        port = p["port"]
        protocol = p["protocol"]
        desc = p.get("description", "")
        
        rule_name = f"WinServerManager_Global_Port_{port}_{protocol}"
        display_name = f"WinServerManager - Global - {port}/{protocol}"
        if desc:
            display_name += f" ({desc})"
            
        expected_rules[rule_name] = (display_name, port, protocol)
        
    # 3. 獲取系統當前已建立的 WinServerManager 規則
    system_rules = list_rules(all_rules=False)
    system_rule_names = {r["name"] for r in system_rules if r["enabled"]}
    
    # 4. 對比並安全補回缺失的規則
    added_count = 0
    failed_count = 0
    for rule_name, (display_name, port, protocol) in expected_rules.items():
        if rule_name not in system_rule_names:
            logger.info(f"[防火牆對齊] 偵測到缺失的防火牆規則，正在補回: {rule_name}")
            success, err_msg = add_rule(rule_name, display_name, port, protocol, enabled=True)
            if success:
                added_count += 1
            else:
                failed_count += 1
                logger.warning(f"[防火牆對齊] 補回規則失敗 {rule_name}: {err_msg}")
                
    if added_count > 0:
        logger.info(f"[防火牆對齊] 檢查結束，自動補回了 {added_count} 條缺失的規則。")
    if failed_count > 0:
        logger.warning(f"[防火牆對齊] 檢查結束，有 {failed_count} 條規則補回失敗（可能是權限不足）。")
    elif added_count == 0:
        logger.info("[防火牆對齊] 檢查結束，未發現任何缺失規則，系統防火牆吻合。")


def start_firewall_reconciliation_loop():
    """
    啟動每小時檢查一次的防火牆對齊背景執行緒
    """
    def loop():
        # 啟動後稍微延遲 15 秒，以避開開機初始化時的其他 IO/背景啟動高峰
        time.sleep(15)
        while True:
            try:
                reconcile_firewall_rules()
            except Exception as e:
                logger.error(f"[防火牆對齊] 背景巡檢發生異常: {e}")
            # 每小時 (3600 秒) 檢查一次
            time.sleep(3600)
            
    t = threading.Thread(target=loop, daemon=True, name="FirewallReconciliation")
    t.start()
