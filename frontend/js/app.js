// 重寫全域 confirm，若帶有 test=true 參數則自動回傳 true 方便自動化測試
if (window.location.search.includes("test=true")) {
    window.confirm = () => true;
}

// 重寫全域 fetch，自動攜帶 Authorization Token 並攔截 401 錯誤
const originalFetch = window.fetch;
window.fetch = async function (url, options = {}) {
    options.headers = options.headers || {};
    const token = localStorage.getItem("admin_token");
    if (token) {
        options.headers["Authorization"] = `Bearer ${token}`;
    }
    
    try {
        const res = await originalFetch(url, options);
        // 若回傳 401 且不是認證 API 本身，表示 Token 已失效，清除並顯示登入
        if (res.status === 401 && 
            !url.includes("/api/auth/login") && 
            !url.includes("/api/auth/setup") && 
            !url.includes("/api/auth/status")) {
            localStorage.removeItem("admin_token");
            showAuthOverlay(false);
        }
        return res;
    } catch (e) {
        throw e;
    }
};

// 全局狀態
let servers = [];
let selectedServerId = null;
let currentPath = "";
let wsConn = null;
let resourceChart = null;
let isAutoScroll = true;
let logPollInterval = null;

// 全局事件日誌快取
let globalEvents = [];

// 圖表歷史數據快取 (最多保留 20 個點)
const maxChartPoints = 20;
const chartLabels = Array(maxChartPoints).fill("");
const chartCpuData = Array(maxChartPoints).fill(0);
const chartRamData = Array(maxChartPoints).fill(0);

// DOM 元素
const globalOfflineBanner = document.getElementById("global-offline-banner");
const sysCpuBar = document.getElementById("sys-cpu-bar");
const sysCpuVal = document.getElementById("sys-cpu-val");
const sysRamBar = document.getElementById("sys-ram-bar");
const sysRamVal = document.getElementById("sys-ram-val");
const sysGpuBar = document.getElementById("sys-gpu-bar");
const sysGpuVal = document.getElementById("sys-gpu-val");

const serverListContainer = document.getElementById("server-list-container");
const welcomePanel = document.getElementById("welcome-panel");
const serverPanel = document.getElementById("server-panel");
const currentServerName = document.getElementById("current-server-name");
const currentServerStatus = document.getElementById("current-server-status");

const btnStartServer = document.getElementById("btn-start-server");
const btnStopServer = document.getElementById("btn-stop-server");
const btnDeleteServer = document.getElementById("btn-delete-server");

const btnShowAddModal = document.getElementById("btn-show-add-modal");
const btnCloseAddModal = document.getElementById("btn-close-add-modal");
const modalAddServer = document.getElementById("modal-add-server");
const btnCancelAdd = document.getElementById("btn-cancel-add");
const btnConfirmAdd = document.getElementById("btn-confirm-add");

const cfgExecutable = document.getElementById("cfg-executable");
const cfgArguments = document.getElementById("cfg-arguments");
const cfgRamLimit = document.getElementById("cfg-ram-limit");
const cfgWatchdog = document.getElementById("cfg-watchdog");
const btnSaveConfig = document.getElementById("btn-save-config");

const terminalOutput = document.getElementById("terminal-output");
const btnClearTerminal = document.getElementById("btn-clear-terminal");
const btnScrollToggle = document.getElementById("btn-scroll-toggle");

const fileBreadcrumbs = document.getElementById("file-breadcrumbs");
const btnFileUp = document.getElementById("btn-file-up");
const btnCreateFolder = document.getElementById("btn-create-folder");
const btnTriggerUpload = document.getElementById("btn-trigger-upload");
const fileUploadInput = document.getElementById("file-upload-input");
const fileDropzone = document.getElementById("file-dropzone");
const uploadProgressWrapper = document.getElementById("upload-progress-wrapper");
const uploadFilename = document.getElementById("upload-filename");
const uploadPercentage = document.getElementById("upload-percentage");
const uploadProgressBar = document.getElementById("upload-progress-bar");
const fileListBody = document.getElementById("file-list-body");

const modalEditor = document.getElementById("modal-editor");
const btnCloseEditorModal = document.getElementById("btn-close-editor-modal");
const editorTitle = document.getElementById("editor-title");
const editorTextarea = document.getElementById("editor-textarea");
const editorStatusText = document.getElementById("editor-status-text");
const btnCancelEditor = document.getElementById("btn-cancel-editor");
const btnSaveEditor = document.getElementById("btn-save-editor");

const logoBtn = document.getElementById("logo-btn");

// 全局設定相關 DOM
const btnShowSettings = document.getElementById("btn-show-settings");
const settingsPanel = document.getElementById("settings-panel");
const sysAutostart = document.getElementById("sys-autostart");
const sysDiscordEnabled = document.getElementById("sys-discord-enabled");
const sysDiscordToken = document.getElementById("sys-discord-token");
const sysDiscordChannel = document.getElementById("sys-discord-channel");
const btnSaveGlobalConfig = document.getElementById("btn-save-global-config");
const btnTestDiscord = document.getElementById("btn-test-discord");
const btnCleanupBackups = document.getElementById("btn-cleanup-backups");

// 終端機指令相關 DOM
const terminalCmdInput = document.getElementById("terminal-cmd-input");
const btnSendCmd = document.getElementById("btn-send-cmd");

// 排程管理相關 DOM
const schedulerListBody = document.getElementById("scheduler-list-body");
const btnAddSchedule = document.getElementById("btn-add-schedule");
const modalScheduler = document.getElementById("modal-scheduler");
const btnCloseSchedulerModal = document.getElementById("btn-close-scheduler-modal");
const btnCancelScheduler = document.getElementById("btn-cancel-scheduler");
const btnConfirmScheduler = document.getElementById("btn-confirm-scheduler");
const schedTaskId = document.getElementById("sched-task-id");
const schedName = document.getElementById("sched-name");
const schedType = document.getElementById("sched-type");
const schedParamWrapper = document.getElementById("sched-param-wrapper");
const schedParam = document.getElementById("sched-param");
const schedTrigger = document.getElementById("sched-trigger");
const schedTimeWrapper = document.getElementById("sched-time-wrapper");
const schedTimeVal = document.getElementById("sched-time-val");
const schedIntervalWrapper = document.getElementById("sched-interval-wrapper");
const schedIntervalVal = document.getElementById("sched-interval-val");
const schedulerModalTitle = document.getElementById("scheduler-modal-title");

// 備份管理相關 DOM
const backupListBody = document.getElementById("backup-list-body");
const btnCreateBackup = document.getElementById("btn-create-backup");

let editingFilePath = ""; // 當前編輯的檔案路徑

// --- 認證 UI 與控制函數 ---
function showAuthOverlay(isSetup = false) {
    const overlay = document.getElementById("auth-overlay");
    const setupWrapper = document.getElementById("auth-setup-wrapper");
    const loginWrapper = document.getElementById("auth-login-wrapper");
    
    if (!overlay) return;
    
    // 清空欄位與錯誤訊息
    document.getElementById("setup-password").value = "";
    document.getElementById("setup-confirm-password").value = "";
    document.getElementById("login-password").value = "";
    document.getElementById("setup-error-msg").classList.add("d-none");
    document.getElementById("login-error-msg").classList.add("d-none");
    
    overlay.classList.remove("d-none");
    
    if (isSetup) {
        setupWrapper.classList.remove("d-none");
        loginWrapper.classList.add("d-none");
    } else {
        setupWrapper.classList.add("d-none");
        loginWrapper.classList.remove("d-none");
    }
}

function hideAuthOverlay() {
    const overlay = document.getElementById("auth-overlay");
    if (overlay) overlay.classList.add("d-none");
}

function triggerShake(elementId) {
    const el = document.getElementById(elementId);
    if (el) {
        el.classList.add("shake");
        setTimeout(() => el.classList.remove("shake"), 400);
    }
}

async function handleSetupPassword() {
    const pw = document.getElementById("setup-password").value.trim();
    const confirmPw = document.getElementById("setup-confirm-password").value.trim();
    const errMsg = document.getElementById("setup-error-msg");
    
    if (pw.length < 6) {
        errMsg.querySelector("span").textContent = "密碼長度至少需為 6 位字元";
        errMsg.classList.remove("d-none");
        triggerShake("auth-setup-wrapper");
        return;
    }
    
    if (pw !== confirmPw) {
        errMsg.querySelector("span").textContent = "兩次輸入的密碼不一致";
        errMsg.classList.remove("d-none");
        triggerShake("auth-setup-wrapper");
        return;
    }
    
    errMsg.classList.add("d-none");
    
    try {
        const res = await originalFetch("/api/auth/setup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: pw })
        });
        
        if (res.ok) {
            const data = await res.json();
            localStorage.setItem("admin_token", data.token);
            hideAuthOverlay();
            // 初始化數據加載
            loadServerList();
            initWebSocket();
            addGlobalEvent("管理密碼設定成功，已登入系統", "info");
        } else {
            const err = await res.json();
            errMsg.querySelector("span").textContent = err.detail || "密碼設定失敗";
            errMsg.classList.remove("d-none");
            triggerShake("auth-setup-wrapper");
        }
    } catch (e) {
        alert("設定密碼時發生網路錯誤");
    }
}

async function handleLogin() {
    const pw = document.getElementById("login-password").value.trim();
    const errMsg = document.getElementById("login-error-msg");
    
    if (!pw) {
        errMsg.querySelector("span").textContent = "請輸入密碼";
        errMsg.classList.remove("d-none");
        triggerShake("auth-login-wrapper");
        return;
    }
    
    try {
        const res = await originalFetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: pw })
        });
        
        if (res.ok) {
            const data = await res.json();
            localStorage.setItem("admin_token", data.token);
            hideAuthOverlay();
            // 初始化數據加載
            loadServerList();
            initWebSocket();
            addGlobalEvent("管理員驗證成功，已登入系統", "info");
        } else {
            const err = await res.json();
            errMsg.querySelector("span").textContent = err.detail || "密碼驗證失敗";
            errMsg.classList.remove("d-none");
            triggerShake("auth-login-wrapper");
        }
    } catch (e) {
        alert("登入時發生網路錯誤");
    }
}

async function handleLogout() {
    if (!confirm("確定要登出系統嗎？")) return;
    try {
        await fetch("/api/auth/logout", { method: "POST" });
    } catch (e) {
        console.error("發送登出請求失敗:", e);
    }
    
    // 清除權杖
    localStorage.removeItem("admin_token");
    
    // 重設狀態
    selectedServerId = null;
    document.querySelectorAll(".server-item").forEach(item => item.classList.remove("active"));
    if (logPollInterval) clearInterval(logPollInterval);
    serverPanel.classList.add("d-none");
    settingsPanel.classList.add("d-none");
    welcomePanel.classList.remove("d-none");
    
    // 關閉即時監控 WebSocket 連線
    if (wsConn) {
        wsConn.onopen = null;
        wsConn.onmessage = null;
        wsConn.onclose = null;
        wsConn.onerror = null;
        wsConn.close();
        wsConn = null;
    }
    
    // 重新顯示登入畫面
    showAuthOverlay(false);
}

// --- 初始化程序 ---
async function init() {
    bindEvents();
    
    try {
        // 先請求確認是否需要設定密碼
        const res = await originalFetch("/api/auth/status");
        if (res.ok) {
            const status = await res.json();
            if (status.setup_required) {
                // 尚未設定密碼，開啟初始設定密碼遮罩
                showAuthOverlay(true);
            } else {
                // 已設定密碼，驗證本地是否有 token
                const token = localStorage.getItem("admin_token");
                if (!token) {
                    showAuthOverlay(false);
                } else {
                    // 已登入，直接開始加載
                    loadServerList();
                    initWebSocket();
                    addGlobalEvent("系統管理中心啟動成功，已建立連線...", "info");
                }
            }
        } else {
            showOfflineBanner();
        }
    } catch (e) {
        console.error("認證狀態檢查失敗:", e);
        showOfflineBanner();
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

// --- 離線提示 Banner 控制輔助函數 (L-3) ---
function showOfflineBanner() {
    if (globalOfflineBanner) globalOfflineBanner.classList.remove("d-none");
}
function hideOfflineBanner() {
    if (globalOfflineBanner) globalOfflineBanner.classList.add("d-none");
}

// --- 事件綁定 ---
function bindEvents() {
    // 標籤頁切換
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
            
            btn.classList.add("active");
            const tabId = btn.getAttribute("data-tab");
            document.getElementById(tabId).classList.add("active");
            
            // L-1: 當切換到非 Dashboard tab 時，暫停日誌輪詢，降低後端與網路負擔
            if (tabId !== "tab-dashboard") {
                if (logPollInterval) {
                    clearInterval(logPollInterval);
                    logPollInterval = null;
                }
            } else {
                // 切換回 Dashboard 時，若已選中伺服器，重新啟動日誌輪詢
                if (selectedServerId) {
                    loadLogs();
                    if (logPollInterval) clearInterval(logPollInterval);
                    logPollInterval = setInterval(loadLogs, 1500);
                }
            }
            
            // 切換至檔案管理時，若路徑未初始化，則加載根目錄
            if (tabId === "tab-files") {
                loadFiles(currentPath);
            }
            // 新增：切換至排程
            else if (tabId === "tab-scheduler") {
                loadSchedulerTasks();
            }
            // 新增：切換至備份
            else if (tabId === "tab-backups") {
                loadBackups();
            }
        });
    });

    // 新增伺服器彈窗控制
    btnShowAddModal.addEventListener("click", () => {
        document.getElementById("new-server-id").value = "";
        document.getElementById("new-server-name").value = "";
        modalAddServer.classList.remove("d-none");
    });
    const hideAddModal = () => modalAddServer.classList.add("d-none");
    btnCloseAddModal.addEventListener("click", hideAddModal);
    btnCancelAdd.addEventListener("click", hideAddModal);
    btnConfirmAdd.addEventListener("click", handleAddServer);

    // 啟動與停止控制
    btnStartServer.addEventListener("click", startServer);
    btnStopServer.addEventListener("click", stopServer);
    btnDeleteServer.addEventListener("click", deleteServer);

    // 儲存設定
    btnSaveConfig.addEventListener("click", saveConfig);

    // 終端機日誌控制
    btnClearTerminal.addEventListener("click", async () => {
        if (!selectedServerId) return;
        try {
            terminalOutput.innerHTML = '<div class="term-line system-msg">[系統] 正在清空控制台日誌...</div>';
            const res = await fetch(`/api/servers/${selectedServerId}/logs`, { method: "DELETE" });
            if (res.ok) {
                terminalOutput.innerHTML = '<div class="term-line system-msg">[系統] 螢幕與後端快取日誌已成功清空。</div>';
            } else {
                const err = await res.json();
                console.error("清空後端日誌失敗:", err.detail);
                terminalOutput.innerHTML = '<div class="term-line system-msg">[系統警告] 清空後端日誌失敗。</div>';
            }
        } catch (e) {
            console.error("清空日誌網路錯誤:", e);
            terminalOutput.innerHTML = '<div class="term-line system-msg">[系統警告] 網路連線錯誤，清空失敗。</div>';
        }
    });
    btnScrollToggle.addEventListener("click", () => {
        isAutoScroll = !isAutoScroll;
        btnScrollToggle.classList.toggle("active", isAutoScroll);
    });
    btnScrollToggle.classList.add("active"); // 預設開啟自動滾動

    // 終端機指令發送控制
    btnSendCmd.addEventListener("click", sendTerminalCommand);
    terminalCmdInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            sendTerminalCommand();
        }
    });

    // 檔案總管控制
    btnFileUp.addEventListener("click", () => {
        if (!currentPath) return;
        const parts = currentPath.split("/");
        parts.pop();
        loadFiles(parts.join("/"));
    });
    btnCreateFolder.addEventListener("click", createFolder);
    btnTriggerUpload.addEventListener("click", () => fileUploadInput.click());
    fileUploadInput.addEventListener("change", (e) => {
        if (e.target.files.length > 0) {
            uploadFiles(e.target.files);
        }
    });

    // 拖放上傳
    fileDropzone.addEventListener("dragover", (e) => {
        e.preventDefault();
        fileDropzone.classList.add("dragover");
    });
    fileDropzone.addEventListener("dragleave", () => {
        fileDropzone.classList.remove("dragover");
    });
    fileDropzone.addEventListener("drop", (e) => {
        e.preventDefault();
        fileDropzone.classList.remove("dragover");
        if (e.dataTransfer.files.length > 0) {
            uploadFiles(e.dataTransfer.files);
        }
    });

    // 編輯器彈窗控制
    const hideEditor = () => modalEditor.classList.add("d-none");
    btnCloseEditorModal.addEventListener("click", hideEditor);
    btnCancelEditor.addEventListener("click", hideEditor);
    btnSaveEditor.addEventListener("click", saveEditedFile);

    // 排程任務彈窗與操作控制
    btnAddSchedule.addEventListener("click", () => openSchedulerModal());
    const hideSchedulerModal = () => modalScheduler.classList.add("d-none");
    btnCloseSchedulerModal.addEventListener("click", hideSchedulerModal);
    btnCancelScheduler.addEventListener("click", hideSchedulerModal);
    btnConfirmScheduler.addEventListener("click", saveSchedulerTask);

    schedType.addEventListener("change", () => {
        if (schedType.value === "command") {
            schedParamWrapper.classList.remove("d-none");
        } else {
            schedParamWrapper.classList.add("d-none");
        }
    });

    schedTrigger.addEventListener("change", () => {
        if (schedTrigger.value === "time") {
            schedTimeWrapper.classList.remove("d-none");
            schedIntervalWrapper.classList.add("d-none");
        } else {
            schedTimeWrapper.classList.add("d-none");
            schedIntervalWrapper.classList.remove("d-none");
        }
    });

    // 立即備份控制
    btnCreateBackup.addEventListener("click", createBackup);

    // 全局設定控制
    btnShowSettings.addEventListener("click", () => {
        selectedServerId = null;
        document.querySelectorAll(".server-item").forEach(item => item.classList.remove("active"));
        if (logPollInterval) clearInterval(logPollInterval);
        serverPanel.classList.add("d-none");
        welcomePanel.classList.add("d-none");
        settingsPanel.classList.remove("d-none");
        loadGlobalConfig();
    });
    btnSaveGlobalConfig.addEventListener("click", saveGlobalConfig);
    btnTestDiscord.addEventListener("click", testDiscordAlert);
    btnCleanupBackups.addEventListener("click", cleanupOrphanBackups);

    // 點選 Logo 回到全局首頁
    logoBtn.addEventListener("click", () => {
        selectedServerId = null;
        document.querySelectorAll(".server-item").forEach(item => item.classList.remove("active"));
        if (logPollInterval) clearInterval(logPollInterval);
        serverPanel.classList.add("d-none");
        settingsPanel.classList.add("d-none");
        welcomePanel.classList.remove("d-none");
        renderGlobalEvents();
    });

    // 綁定認證相關事件
    document.querySelectorAll(".toggle-password").forEach(icon => {
        icon.addEventListener("click", () => {
            const targetId = icon.getAttribute("data-target");
            const input = document.getElementById(targetId);
            if (input.type === "password") {
                input.type = "text";
                icon.classList.remove("fa-eye-slash");
                icon.classList.add("fa-eye");
            } else {
                input.type = "password";
                icon.classList.remove("fa-eye");
                icon.classList.add("fa-eye-slash");
            }
        });
    });

    document.getElementById("btn-submit-setup").addEventListener("click", handleSetupPassword);
    document.getElementById("setup-password").addEventListener("keydown", (e) => { if (e.key === "Enter") handleSetupPassword(); });
    document.getElementById("setup-confirm-password").addEventListener("keydown", (e) => { if (e.key === "Enter") handleSetupPassword(); });

    document.getElementById("btn-submit-login").addEventListener("click", handleLogin);
    document.getElementById("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") handleLogin(); });

    document.getElementById("btn-logout").addEventListener("click", handleLogout);
}

// --- API 請求與數據載入 ---

// 1. 載入伺服器列表
async function loadServerList() {
    try {
        const res = await fetch("/api/servers");
        if (res.ok) {
            servers = await res.json();
            renderServerList();
            hideOfflineBanner(); // 載入成功，隱藏離線 Banner (L-3)
        } else {
            showOfflineBanner();
        }
    } catch (e) {
        console.error("載入伺服器列表失敗", e);
        showOfflineBanner(); // 載入失敗，顯示離線 Banner (L-3)
    }
}

// 2. 渲染伺服器列表 (M-2: 使用 textContent 防止 XSS)
function renderServerList() {
    serverListContainer.innerHTML = "";
    if (servers.length === 0) {
        serverListContainer.innerHTML = '<div class="list-empty">尚無伺服器，點擊「新增」來建立。</div>';
        return;
    }
    
    servers.forEach(server => {
        const item = document.createElement("div");
        item.className = `server-item ${server.server_id === selectedServerId ? 'active' : ''}`;
        item.addEventListener("click", () => selectServer(server.server_id));
        
        const statusClass = server.is_running ? 'running' : 'stopped';
        
        // 使用 createElement + textContent 防止 XSS
        const topRow = document.createElement("div");
        topRow.className = "server-item-top";
        
        const nameSpan = document.createElement("span");
        nameSpan.className = "server-item-name";
        nameSpan.textContent = server.name;
        
        const indicatorSpan = document.createElement("span");
        indicatorSpan.className = `status-indicator ${statusClass}`;
        
        topRow.appendChild(nameSpan);
        topRow.appendChild(indicatorSpan);
        
        const bottomRow = document.createElement("div");
        bottomRow.className = "server-item-top";
        
        const idSpan = document.createElement("span");
        idSpan.className = "server-item-id";
        idSpan.textContent = `ID: ${server.server_id}`;
        
        const statsSpan = document.createElement("span");
        statsSpan.className = "server-item-stats";
        
        const cpuSpan = document.createElement("span");
        cpuSpan.innerHTML = `<i class="fa-solid fa-microchip"></i> `;
        const cpuText = document.createTextNode(server.is_running ? server.cpu + '%' : '-');
        cpuSpan.appendChild(cpuText);
        
        const ramSpan = document.createElement("span");
        ramSpan.innerHTML = `<i class="fa-solid fa-memory"></i> `;
        const ramText = document.createTextNode(server.is_running ? server.ram + 'M' : '-');
        ramSpan.appendChild(ramText);
        
        statsSpan.appendChild(cpuSpan);
        statsSpan.appendChild(ramSpan);
        
        bottomRow.appendChild(idSpan);
        bottomRow.appendChild(statsSpan);
        
        item.appendChild(topRow);
        item.appendChild(bottomRow);
        serverListContainer.appendChild(item);
    });
}

// 3. 選擇伺服器
async function selectServer(serverId) {
    // H-2: 立即清除所有舊計時器，防止快速點擊伺服器時重複請求
    if (logPollInterval) {
        clearInterval(logPollInterval);
        logPollInterval = null;
    }
    selectedServerId = serverId;
    currentPath = ""; // 重設為根目錄
    
    // 更新列表高亮
    document.querySelectorAll(".server-item").forEach(item => {
        item.classList.remove("active");
    });
    loadServerList(); // 重新拉取以確保最新狀態
    
    const server = servers.find(s => s.server_id === serverId);
    if (!server) return;
    
    // 切換工作區面板
    welcomePanel.classList.add("d-none");
    settingsPanel.classList.add("d-none"); // 確保設定面板被隱藏 (防止與伺服器面板堆疊)
    serverPanel.classList.remove("d-none");
    
    currentServerName.innerText = server.name;
    updateStatusUI(server.is_running);
    
    // 填入設定表單
    cfgExecutable.value = server.executable || "";
    cfgArguments.value = server.arguments || "";
    cfgRamLimit.value = server.ram_limit_mb || 0;
    cfgWatchdog.checked = server.watchdog_enabled || false;
    
    // 初始化/重設效能折線圖
    initChart();
    
    // 獲取並載入日誌
    loadLogs();
    
    // 啟動日誌定時輪詢
    if (logPollInterval) clearInterval(logPollInterval);
    logPollInterval = setInterval(loadLogs, 1500);

    // 切換預設標籤頁
    document.querySelector('[data-tab="tab-dashboard"]').click();
}

// 更新伺服器狀態 UI
function updateStatusUI(isRunning) {
    if (isRunning) {
        currentServerStatus.innerText = "運行中";
        currentServerStatus.className = "badge badge-success";
        btnStartServer.classList.add("d-none");
        btnStopServer.classList.remove("d-none");
        terminalCmdInput.disabled = false;
        btnSendCmd.disabled = false;
    } else {
        currentServerStatus.innerText = "已停止";
        currentServerStatus.className = "badge badge-danger";
        btnStartServer.classList.remove("d-none");
        btnStopServer.classList.add("d-none");
        terminalCmdInput.disabled = true;
        btnSendCmd.disabled = true;
    }
}

// 4. 新增伺服器
async function handleAddServer() {
    const id = document.getElementById("new-server-id").value.strip();
    const name = document.getElementById("new-server-name").value.strip();
    
    if (!id || !name) {
        alert("請填寫所有欄位");
        return;
    }
    
    try {
        const res = await fetch("/api/servers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ server_id: id, name: name })
        });
        
        if (res.ok) {
            modalAddServer.classList.add("d-none");
            loadServerList();
            setTimeout(() => selectServer(id), 300);
        } else {
            const err = await res.json();
            alert(`建立失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("網路錯誤");
    }
}

// 5. 儲存伺服器啟動設定
async function saveConfig() {
    if (!selectedServerId) return;
    
    const reqData = {
        executable: cfgExecutable.value.trim(),
        arguments: cfgArguments.value.trim(),
        ram_limit_mb: parseInt(cfgRamLimit.value) || 0,
        watchdog_enabled: cfgWatchdog.checked
    };
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(reqData)
        });
        
        if (res.ok) {
            appendSystemLog("[系統提示] 設定儲存成功。");
            loadServerList();
        } else {
            const err = await res.json();
            alert(`儲存設定失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("儲存設定時發生網路錯誤");
    }
}

// 6. 啟動伺服器
async function startServer() {
    if (!selectedServerId) return;
    
    // 檢查是否有設定執行檔路徑，若無則主動阻擋並提示
    const server = servers.find(s => s.server_id === selectedServerId);
    if (server && (!server.executable || !server.executable.trim())) {
        alert("啟動失敗：尚未設定該伺服器的「執行檔路徑」。\n請先在「啟動與安全設定」中填寫執行檔路徑（如: server.exe）並點擊儲存，再點擊啟動！");
        return;
    }
    
    appendSystemLog("[系統指令] 發送啟動命令...");
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/start`, { method: "POST" });
        if (res.ok) {
            updateStatusUI(true);
            loadServerList();
        } else {
            const err = await res.json();
            appendSystemLog(`[系統錯誤] 啟動失敗: ${err.detail}`, true);
            alert(`啟動伺服器失敗: ${err.detail}`);
        }
    } catch (e) {
        appendSystemLog("[系統錯誤] 網路連線失敗", true);
    }
}

// 7. 停止伺服器
async function stopServer() {
    if (!selectedServerId) return;
    appendSystemLog("[系統指令] 發送停止命令...");
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/stop`, { method: "POST" });
        if (res.ok) {
            updateStatusUI(false);
            loadServerList();
        } else {
            const err = await res.json();
            appendSystemLog(`[系統錯誤] 停止失敗: ${err.detail}`, true);
            alert(`停止伺服器失敗: ${err.detail}`);
        }
    } catch (e) {
        appendSystemLog("[系統錯誤] 網路連線失敗", true);
    }
}

// 8. 刪除伺服器
async function deleteServer() {
    if (!selectedServerId) return;
    if (!confirm("確定要刪除此伺服器實例與其底下的所有檔案嗎？此操作不可逆！")) return;
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}`, { method: "DELETE" });
        if (res.ok) {
            if (logPollInterval) clearInterval(logPollInterval);
            selectedServerId = null;
            welcomePanel.classList.remove("d-none");
            serverPanel.classList.add("d-none");
            loadServerList();
        } else {
            const err = await res.json();
            alert(`刪除失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("刪除失敗，網路錯誤");
    }
}

// 9. 載入並渲染日誌
async function loadLogs() {
    if (!selectedServerId) return;
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/logs`);
        if (res.ok) {
            const logs = await res.json();
            renderLogs(logs);
        }
    } catch (e) {
        console.error("載入日誌失敗", e);
    }
}

function renderLogs(logs) {
    terminalOutput.innerHTML = "";
    if (logs.length === 0) {
        terminalOutput.innerHTML = '<div class="term-line system-msg">[系統] 暫無控制台日誌。</div>';
        return;
    }
    
    logs.forEach(line => {
        const lineEl = document.createElement("div");
        lineEl.className = "term-line";
        
        // 區分系統警告與普通日誌顏色
        if (line.includes("[系統錯誤]") || line.includes("[看門狗警告]")) {
            lineEl.classList.add("error-msg");
        } else if (line.includes("[系統資訊]") || line.includes("[系統警告]")) {
            lineEl.classList.add("system-msg");
        }
        
        lineEl.innerText = line;
        terminalOutput.appendChild(lineEl);
    });
    
    if (isAutoScroll) {
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }
}

function appendSystemLog(text, isError = false) {
    const timeStr = new Date().toLocaleTimeString();
    const lineEl = document.createElement("div");
    lineEl.className = `term-line ${isError ? 'error-msg' : 'system-msg'}`;
    lineEl.innerText = `[${timeStr}] ${text}`;
    terminalOutput.appendChild(lineEl);
    if (isAutoScroll) {
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }
}

function initWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const token = localStorage.getItem("admin_token") || "";
    const wsUrl = `${protocol}//${window.location.host}/ws/monitor?token=${encodeURIComponent(token)}`;
    
    wsConn = new WebSocket(wsUrl);
    
    wsConn.onopen = () => {
        hideOfflineBanner(); // 連線成功，隱藏離線 Banner (L-3)
    };
    
    wsConn.onmessage = (event) => {
        hideOfflineBanner(); // 有收到訊息，說明連線暢通 (L-3)
        const data = JSON.parse(event.data);
        
        // 1. 更新頂端全局系統指標
        updateSystemBar(data.system);
        
        // 2. 比對狀態，產生全局事件日誌
        if (window.lastServersState) {
            for (const sId in data.servers) {
                const old = window.lastServersState[sId];
                const cur = data.servers[sId];
                if (old) {
                    const sName = (servers.find(s => s.server_id === sId) || { name: sId }).name;
                    if (!old.is_running && cur.is_running) {
                        addGlobalEvent(`伺服器 [${sName}] 已成功啟動運行`, "info");
                    } else if (old.is_running && !cur.is_running) {
                        addGlobalEvent(`伺服器 [${sName}] 已停止`, "warning");
                    }
                    if (cur.restart_count > old.restart_count) {
                        addGlobalEvent(`伺服器 [${sName}] 偵測到異常結束，看門狗已執行防護重啟（累計: ${cur.restart_count} 次）`, "danger");
                    }
                }
            }
        }
        window.lastServersState = data.servers;
        
        // 3. 如果當前選中了某個伺服器，更新圖表與狀態
        if (selectedServerId && data.servers[selectedServerId]) {
            const sData = data.servers[selectedServerId];
            updateStatusUI(sData.is_running);
            
            // 繪製資源折線圖
            updateChartData(sData.cpu, sData.ram);
        } else if (!selectedServerId) {
            // 若在首頁，更新全局監控中心看板
            updateGlobalDashboard(data);
        }
    };
    
    wsConn.onclose = (event) => {
        showOfflineBanner(); // 連線關閉，顯示離線 Banner (L-3)
        // H-3: 清空舊連線的事件回呼，防止殭屍回呼與記憶體洩漏
        if (wsConn) {
            wsConn.onopen = null;
            wsConn.onmessage = null;
            wsConn.onclose = null;
            wsConn.onerror = null;
            wsConn = null;
        }
        // 若是因為未授權被關閉 (1008 Policy Violation)
        if (event && event.code === 1008) {
            localStorage.removeItem("admin_token");
            showAuthOverlay(false);
            return;
        }
        console.log("WebSocket 已關閉，嘗試在 5 秒後重連...");
        setTimeout(initWebSocket, 5000);
    };
    
    wsConn.onerror = (e) => {
        showOfflineBanner(); // 連線出錯，顯示離線 Banner (L-3)
        console.error("WebSocket 連線錯誤", e);
    };
}

function updateSystemBar(systemStats) {
    sysCpuBar.style.width = `${systemStats.cpu}%`;
    sysCpuVal.innerText = `${systemStats.cpu}%`;
    
    sysRamBar.style.width = `${systemStats.ram}%`;
    sysRamVal.innerText = `${systemStats.ram}%`;
    
    sysGpuBar.style.width = `${systemStats.gpu}%`;
    sysGpuVal.innerText = `${systemStats.gpu}%`;
}

// --- Chart.js 折線圖配置 ---
function initChart() {
    if (resourceChart) {
        resourceChart.destroy();
    }
    
    // 清空歷史快取
    chartCpuData.fill(0);
    chartRamData.fill(0);
    
    const ctx = document.getElementById("resourceChart").getContext("2d");
    resourceChart = new Chart(ctx, {
        type: "line",
        data: {
            labels: chartLabels,
            datasets: [
                {
                    label: "CPU 使用率 (%)",
                    data: chartCpuData,
                    borderColor: "#6366f1",
                    backgroundColor: "rgba(99, 102, 241, 0.1)",
                    borderWidth: 2,
                    tension: 0.3,
                    fill: true
                },
                {
                    label: "記憶體使用 (MB)",
                    data: chartRamData,
                    borderColor: "#a855f7",
                    backgroundColor: "rgba(168, 85, 247, 0.1)",
                    borderWidth: 2,
                    tension: 0.3,
                    fill: true,
                    yAxisID: "y-ram"
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            scales: {
                x: {
                    grid: { display: false }
                },
                y: {
                    type: "linear",
                    display: true,
                    position: "left",
                    min: 0,
                    max: 100,
                    ticks: {
                        callback: (value) => value + "%"
                    },
                    grid: {
                        color: "rgba(255, 255, 255, 0.05)"
                    }
                },
                "y-ram": {
                    type: "linear",
                    display: true,
                    position: "right",
                    min: 0,
                    grid: { drawOnChartArea: false }
                }
            },
            plugins: {
                legend: {
                    labels: { color: "#f8fafc" }
                }
            }
        }
    });
}

function updateChartData(cpu, ram) {
    if (!resourceChart) return;
    
    const nowStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    
    // 將新值塞入並踢掉最舊的
    chartCpuData.push(cpu);
    chartCpuData.shift();
    
    chartRamData.push(ram);
    chartRamData.shift();
    
    chartLabels.push(nowStr);
    chartLabels.shift();
    
    resourceChart.update();
}

// --- 檔案總管邏輯 ---

// 1. 載入指定相對路徑的檔案
async function loadFiles(path) {
    if (!selectedServerId) return;
    currentPath = path;
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files?path=${encodeURIComponent(path)}`);
        if (res.ok) {
            const files = await res.json();
            renderFileList(files);
            renderBreadcrumbs();
        } else {
            const err = await res.json();
            alert(`載入檔案失敗: ${err.detail}`);
        }
    } catch (e) {
        console.error("讀取目錄失敗", e);
    }
}

// 2. 渲染麵包屑
function renderBreadcrumbs() {
    fileBreadcrumbs.innerHTML = "";
    
    // 根目錄項
    const rootItem = document.createElement("span");
    rootItem.className = "breadcrumb-item";
    rootItem.innerText = "根目錄";
    rootItem.addEventListener("click", () => loadFiles(""));
    fileBreadcrumbs.appendChild(rootItem);
    
    if (currentPath) {
        const parts = currentPath.split("/");
        let cumulativePath = "";
        
        parts.forEach((part, index) => {
            if (!part) return;
            cumulativePath += (cumulativePath ? "/" : "") + part;
            
            const item = document.createElement("span");
            item.className = "breadcrumb-item";
            item.innerText = part;
            
            // 閉包綁定當前路徑
            const thisPath = cumulativePath;
            item.addEventListener("click", () => loadFiles(thisPath));
            fileBreadcrumbs.appendChild(item);
        });
    }
    
    // 根據是否在根目錄決定「回上層」按鈕是否禁用
    btnFileUp.disabled = (currentPath === "");
}

// 3. 渲染檔案清單表格
function renderFileList(files) {
    fileListBody.innerHTML = "";
    
    if (files.length === 0) {
        fileListBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 30px;">本目錄無任何檔案。</td></tr>';
        return;
    }
    
    files.forEach(file => {
        const row = document.createElement("tr");
        
        // 檔案名稱與圖示
        let iconHtml = '<i class="fa-solid fa-file file-icon-file"></i>';
        if (file.is_dir) {
            iconHtml = '<i class="fa-solid fa-folder file-icon-dir"></i>';
        } else if (file.name.toLowerCase().endswith?.(".zip") || file.name.toLowerCase().endsWith(".zip")) {
            iconHtml = '<i class="fa-solid fa-file-zipper file-icon-zip"></i>';
        }
        
        const nameCell = document.createElement("td");
        const nameLink = document.createElement("span");
        nameLink.className = "file-row-name";
        nameLink.innerHTML = `${iconHtml} <span>${file.name}</span>`;
        nameLink.addEventListener("click", () => {
            if (file.is_dir) {
                loadFiles(file.path);
            } else if (isEditableFile(file.name)) {
                openEditor(file.path);
            }
        });
        nameCell.appendChild(nameLink);
        
        // 大小與修改時間
        const sizeCell = document.createElement("td");
        sizeCell.innerText = file.is_dir ? "-" : formatBytes(file.size);
        
        const mtimeCell = document.createElement("td");
        mtimeCell.innerText = new Date(file.mtime * 1000).toLocaleString();
        
        // 動作按鈕
        const actionCell = document.createElement("td");
        actionCell.className = "file-row-actions";
        
        // 編輯按鈕 (若為文字檔)
        let editBtnHtml = "";
        if (!file.is_dir && isEditableFile(file.name)) {
            const editBtn = document.createElement("button");
            editBtn.className = "btn btn-icon btn-sm";
            editBtn.title = "線上編輯";
            editBtn.innerHTML = '<i class="fa-solid fa-pen-to-square"></i>';
            editBtn.addEventListener("click", () => openEditor(file.path));
            actionCell.appendChild(editBtn);
        }
        
        // 命名按鈕
        const renameBtn = document.createElement("button");
        renameBtn.className = "btn btn-icon btn-sm";
        renameBtn.title = "重新命名";
        renameBtn.innerHTML = '<i class="fa-solid fa-pen-nib"></i>';
        renameBtn.addEventListener("click", () => renameFile(file.path, file.name));
        actionCell.appendChild(renameBtn);
        
        // 刪除按鈕
        const deleteBtn = document.createElement("button");
        deleteBtn.className = "btn btn-icon btn-sm";
        deleteBtn.title = "刪除";
        deleteBtn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
        deleteBtn.addEventListener("click", () => deleteFile(file.path, file.name));
        actionCell.appendChild(deleteBtn);
        
        row.appendChild(nameCell);
        row.appendChild(sizeCell);
        row.appendChild(mtimeCell);
        row.appendChild(actionCell);
        
        fileListBody.appendChild(row);
    });
}

// 4. 新增資料夾
async function createFolder() {
    if (!selectedServerId) return;
    const folderName = prompt("請輸入新資料夾名稱:");
    if (!folderName) return;
    
    // 安全檢測名稱，防止斜槓
    if (folderName.includes("/") || folderName.includes("\\")) {
        alert("資料夾名稱不能包含路徑斜線");
        return;
    }
    
    const relativePath = currentPath ? `${currentPath}/${folderName}` : folderName;
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files/action`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "mkdir", path: relativePath })
        });
        if (res.ok) {
            loadFiles(currentPath);
        } else {
            const err = await res.json();
            alert(`建立資料夾失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("操作失敗");
    }
}

// 5. 檔案重新命名
async function renameFile(oldPath, oldName) {
    if (!selectedServerId) return;
    const newName = prompt("請輸入新名稱:", oldName);
    if (!newName || newName === oldName) return;
    
    if (newName.includes("/") || newName.includes("\\")) {
        alert("名稱不能包含路徑斜線");
        return;
    }
    
    // 組合新路徑
    const pathParts = oldPath.split("/");
    pathParts.pop(); // 拿掉舊檔名
    pathParts.push(newName);
    const newPath = pathParts.join("/");
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files/action`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "rename", path: oldPath, new_path: newPath })
        });
        if (res.ok) {
            loadFiles(currentPath);
        } else {
            const err = await res.json();
            alert(`重新命名失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("操作失敗");
    }
}

// 6. 刪除檔案或資料夾
async function deleteFile(path, name) {
    if (!selectedServerId) return;
    if (!confirm(`確定要刪除「${name}」嗎？此操作無法還原。`)) return;
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files/action`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "delete", path: path })
        });
        if (res.ok) {
            loadFiles(currentPath);
        } else {
            const err = await res.json();
            alert(`刪除失敗: ${err.detail}`);
        }
    } catch (e) {
        alert("操作失敗");
    }
}

// 7. 線上文字編輯器
async function openEditor(path) {
    if (!selectedServerId) return;
    editingFilePath = path;
    
    // 顯示載入中
    editorTitle.innerText = `編輯檔案: ${path.split("/").pop()}`;
    editorTextarea.value = "載入中...";
    editorStatusText.innerText = "載入中...";
    modalEditor.classList.remove("d-none");
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files/read?path=${encodeURIComponent(path)}`);
        if (res.ok) {
            const data = await res.json();
            editorTextarea.value = data.content;
            editorStatusText.innerText = "檔案載入成功，可進行修改";
        } else {
            const err = await res.json();
            editorTextarea.value = `載入失敗: ${err.detail}`;
            editorStatusText.innerText = "載入失敗";
        }
    } catch (e) {
        editorTextarea.value = "連線失敗";
        editorStatusText.innerText = "連線失敗";
    }
}

async function saveEditedFile() {
    if (!selectedServerId || !editingFilePath) return;
    editorStatusText.innerText = "儲存中...";
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/files/action`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                action: "write",
                path: editingFilePath,
                content: editorTextarea.value
            })
        });
        
        if (res.ok) {
            editorStatusText.innerText = "儲存成功";
            setTimeout(() => modalEditor.classList.add("d-none"), 500);
            loadFiles(currentPath);
        } else {
            const err = await res.json();
            editorStatusText.innerText = `儲存失敗: ${err.detail}`;
        }
    } catch (e) {
        editorStatusText.innerText = "網路錯誤，儲存失敗";
    }
}

// 8. 檔案上傳（XHR 帶進度條）
function uploadFiles(fileList) {
    if (!selectedServerId) return;
    
    // 一次處理一個檔案（若有多檔則循序或顯示第一個，此處展示第一個檔案，並支持打包 zip 上傳）
    const file = fileList[0];
    
    // 顯示進度條 UI
    uploadFilename.innerText = file.name;
    uploadPercentage.innerText = "0%";
    uploadProgressBar.style.width = "0%";
    uploadProgressWrapper.classList.remove("d-none");
    
    const xhr = new XMLHttpRequest();
    
    // 監聽進度事件
    xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            uploadPercentage.innerText = `${percent}%`;
            uploadProgressBar.style.width = `${percent}%`;
        }
    });
    
    // 請求完成回調
    xhr.onreadystatechange = () => {
        if (xhr.readyState === 4) {
            // 延遲隱藏進度條，提升視覺回饋
            setTimeout(() => {
                uploadProgressWrapper.classList.add("d-none");
            }, 1000);
            
            if (xhr.status === 200) {
                appendSystemLog(`[檔案系統] 檔案上傳成功: ${file.name}`);
                loadFiles(currentPath);
            } else {
                let errMsg = "上傳失敗";
                try {
                    const err = JSON.parse(xhr.responseText);
                    errMsg = err.detail || errMsg;
                } catch(errJson) {}
                alert(`上傳失敗: ${errMsg}`);
                appendSystemLog(`[檔案系統錯誤] 檔案上傳失敗: ${file.name}`, true);
            }
        }
    };
    
    xhr.open("POST", `/api/servers/${selectedServerId}/files/upload`, true);
    
    // 手動攜帶 Authorization Token 以通過全域安全驗證中間件
    const token = localStorage.getItem("admin_token");
    if (token) {
        xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }
    
    const fd = new FormData();
    fd.append("file", file);
    fd.append("relative_path", currentPath);
    
    xhr.send(fd);
}

// --- 輔助函數 ---

// 檢查是否為可編輯的文字檔案
function isEditableFile(filename) {
    const ext = filename.split(".").pop().toLowerCase();
    const textExts = ["txt", "json", "conf", "ini", "bat", "py", "js", "css", "html", "sh", "properties", "cfg", "yml", "yaml", "xml", "log"];
    return textExts.includes(ext);
}

// 格式化檔案大小
function formatBytes(bytes) {
    if (bytes === 0) return "0 Bytes";
    const k = 1024;
    const sizes = ["Bytes", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}

// String.prototype.strip (去除首尾空白)
String.prototype.strip = function() {
    return this.replace(/^\s+|\s+$/g, '');
};

// --- 全局數據監控中心輔助函數 ---

// 全局事件日誌記錄
function addGlobalEvent(text, type = "info") {
    const timeStr = new Date().toLocaleTimeString();
    globalEvents.unshift({ time: timeStr, text: text, type: type });
    if (globalEvents.length > 50) globalEvents.pop(); // 最多保留 50 筆
    renderGlobalEvents();
}

function renderGlobalEvents() {
    const container = document.getElementById("global-log-container");
    if (!container) return;
    container.innerHTML = "";
    if (globalEvents.length === 0) {
        container.innerHTML = '<div class="global-log-line text-muted">[系統] 等待事件中...</div>';
        return;
    }
    globalEvents.forEach(evt => {
        const line = document.createElement("div");
        line.className = `global-log-line system-${evt.type}`;
        line.innerText = `[${evt.time}] ${evt.text}`;
        container.appendChild(line);
    });
}

// 更新 SVG 圓環進度條
function updateGauge(gaugeId, percent) {
    const fill = document.getElementById(`${gaugeId}-fill`);
    const text = document.getElementById(`${gaugeId}-text`);
    if (fill && text) {
        const dasharray = 251; // 2 * PI * r = 2 * 3.14159 * 40 ≈ 251.2
        const offset = dasharray - (percent / 100) * dasharray;
        fill.style.strokeDashoffset = offset;
        text.innerText = `${percent}%`;
    }
}

// 更新全局儀表板視圖
function updateGlobalDashboard(data) {
    // 1. 更新卡片統計
    const totalCount = servers.length;
    const runningCount = Object.values(data.servers).filter(s => s.is_running).length;
    const stoppedCount = totalCount - runningCount;
    const watchdogCount = Object.values(data.servers).filter(s => s.watchdog_enabled).length;

    const elTotal = document.getElementById("global-total-count");
    const elRunning = document.getElementById("global-running-count");
    const elStopped = document.getElementById("global-stopped-count");
    const elWatchdog = document.getElementById("global-watchdog-count");

    if (elTotal) elTotal.innerText = totalCount;
    if (elRunning) elRunning.innerText = runningCount;
    if (elStopped) elStopped.innerText = stoppedCount;
    if (elWatchdog) elWatchdog.innerText = watchdogCount;

    // 2. 更新環狀圖表
    updateGauge("gauge-cpu", data.system.cpu);
    updateGauge("gauge-ram", data.system.ram);
    updateGauge("gauge-gpu", data.system.gpu);

    // 3. 更新伺服器狀態列表表格
    const tableBody = document.getElementById("dashboard-servers-body");
    if (tableBody) {
        tableBody.innerHTML = "";
        servers.forEach(server => {
            const curState = data.servers[server.server_id] || { is_running: false, cpu: 0, ram: 0 };
            const row = document.createElement("tr");
            
            const nameCell = document.createElement("td");
            nameCell.innerText = server.name;
            nameCell.style.fontWeight = "600";
            nameCell.style.cursor = "pointer";
            nameCell.addEventListener("click", () => selectServer(server.server_id));
            
            const statusCell = document.createElement("td");
            const statusClass = curState.is_running ? "badge-success" : "badge-danger";
            const statusText = curState.is_running ? "運行中" : "已停止";
            statusCell.innerHTML = `<span class="badge ${statusClass}">${statusText}</span>`;
            
            const cpuCell = document.createElement("td");
            cpuCell.innerText = curState.is_running ? curState.cpu + "%" : "-";
            
            const ramCell = document.createElement("td");
            ramCell.innerText = curState.is_running ? curState.ram + " MB" : "-";
            
            const actionCell = document.createElement("td");
            const manageBtn = document.createElement("button");
            manageBtn.className = "btn btn-outline btn-sm";
            manageBtn.innerHTML = '<i class="fa-solid fa-gear"></i> 管理';
            manageBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                selectServer(server.server_id);
            });
            actionCell.appendChild(manageBtn);
            
            row.appendChild(nameCell);
            row.appendChild(statusCell);
            row.appendChild(cpuCell);
            row.appendChild(ramCell);
            row.appendChild(actionCell);
            tableBody.appendChild(row);
        });
        if (servers.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding: 20px;">尚無任何伺服器實例。</td></tr>';
        }
    }
}

// ==========================================
// 新增擴充功能實作：控制台發送指令、排程任務、備份系統、Discord設定
// ==========================================

// 1. 控制台互動指令發送
async function sendTerminalCommand() {
    if (!selectedServerId) return;
    const cmd = terminalCmdInput.value.trim();
    if (!cmd) return;
    
    appendUserCmdLog(cmd);
    terminalCmdInput.value = "";
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/input`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command: cmd })
        });
        if (!res.ok) {
            const err = await res.json();
            appendSystemLog(`[系統錯誤] 指令發送失敗: ${err.detail}`, true);
        }
    } catch(e) {
        appendSystemLog(`[系統錯誤] 網路連線失敗`, true);
    }
}

function appendUserCmdLog(cmd) {
    const timeStr = new Date().toLocaleTimeString();
    const lineEl = document.createElement("div");
    lineEl.className = "term-line";
    lineEl.style.color = "var(--accent-purple)"; // 用紫色來標示使用者指令
    lineEl.innerText = `[${timeStr}] > ${cmd}`;
    terminalOutput.appendChild(lineEl);
    if (isAutoScroll) {
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }
}

// 2. 定時任務排程管理
let currentSchedulerTasks = [];

async function loadSchedulerTasks() {
    if (!selectedServerId) return;
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/scheduler`);
        if (res.ok) {
            currentSchedulerTasks = await res.json();
            renderSchedulerTasks();
        }
    } catch(e) {
        console.error("載入定時任務失敗", e);
    }
}

function renderSchedulerTasks() {
    const body = document.getElementById("scheduler-list-body");
    if (!body) return;
    body.innerHTML = "";
    
    if (currentSchedulerTasks.length === 0) {
        body.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 30px;">尚無任何定時排程任務。</td></tr>';
        return;
    }
    
    currentSchedulerTasks.forEach((task, idx) => {
        const row = document.createElement("tr");
        
        const nameCell = document.createElement("td");
        nameCell.innerText = task.name;
        nameCell.style.fontWeight = "600";
        
        const typeCell = document.createElement("td");
        let typeBadge = "";
        if (task.type === "restart") typeBadge = '<span class="badge badge-warning">重啟伺服器</span>';
        else if (task.type === "backup") typeBadge = '<span class="badge badge-purple">執行備份</span>';
        else if (task.type === "command") typeBadge = `<span class="badge badge-secondary" title="${task.param}">執行指令: ${task.param}</span>`;
        typeCell.innerHTML = typeBadge;
        
        const triggerCell = document.createElement("td");
        if (task.trigger === "time") {
            triggerCell.innerText = `每天 ${task.value}`;
        } else {
            triggerCell.innerText = `每隔 ${task.value} 分鐘`;
        }
        
        const statusCell = document.createElement("td");
        const statusClass = task.enabled ? "badge-success" : "badge-secondary";
        const statusText = task.enabled ? "已啟用" : "已停用";
        statusCell.innerHTML = `<span class="badge ${statusClass}">${statusText}</span>`;
        
        const actionCell = document.createElement("td");
        actionCell.className = "file-row-actions";
        
        // 啟用/停用按鈕
        const toggleBtn = document.createElement("button");
        toggleBtn.className = "btn btn-outline btn-sm";
        toggleBtn.innerText = task.enabled ? "停用" : "啟用";
        toggleBtn.addEventListener("click", () => toggleTaskEnabled(task));
        actionCell.appendChild(toggleBtn);
        
        // 編輯按鈕
        const editBtn = document.createElement("button");
        editBtn.className = "btn btn-icon btn-sm";
        editBtn.innerHTML = '<i class="fa-solid fa-pen-to-square"></i>';
        editBtn.title = "編輯任務";
        editBtn.addEventListener("click", () => openSchedulerModal(task));
        actionCell.appendChild(editBtn);
        
        // 刪除按鈕
        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-icon btn-sm";
        delBtn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
        delBtn.title = "刪除任務";
        delBtn.addEventListener("click", () => deleteSchedulerTask(task.task_id));
        actionCell.appendChild(delBtn);
        
        row.appendChild(nameCell);
        row.appendChild(typeCell);
        row.appendChild(triggerCell);
        row.appendChild(statusCell);
        row.appendChild(actionCell);
        body.appendChild(row);
    });
}

function openSchedulerModal(task = null) {
    const idInput = document.getElementById("sched-task-id");
    const nameInput = document.getElementById("sched-name");
    const typeSelect = document.getElementById("sched-type");
    const paramInput = document.getElementById("sched-param");
    const triggerSelect = document.getElementById("sched-trigger");
    const timeVal = document.getElementById("sched-time-val");
    const intervalVal = document.getElementById("sched-interval-val");
    
    if (task) {
        schedulerModalTitle.innerText = "修改定時任務";
        idInput.value = task.task_id;
        nameInput.value = task.name;
        typeSelect.value = task.type;
        paramInput.value = task.param || "";
        triggerSelect.value = task.trigger;
        
        if (task.trigger === "time") {
            timeVal.value = task.value;
            intervalVal.value = "60";
        } else {
            timeVal.value = "03:00";
            intervalVal.value = task.value;
        }
    } else {
        schedulerModalTitle.innerText = "新增定時任務";
        idInput.value = "";
        nameInput.value = "";
        typeSelect.value = "restart";
        paramInput.value = "";
        triggerSelect.value = "time";
        timeVal.value = "03:00";
        intervalVal.value = "60";
    }
    
    // 觸發連動欄位顯示
    schedType.dispatchEvent(new Event("change"));
    schedTrigger.dispatchEvent(new Event("change"));
    
    modalScheduler.classList.remove("d-none");
}

async function saveSchedulerTask() {
    if (!selectedServerId) return;
    
    const idInput = document.getElementById("sched-task-id").value;
    const nameInput = document.getElementById("sched-name").value.trim();
    const typeSelect = document.getElementById("sched-type").value;
    const paramInput = document.getElementById("sched-param").value.trim();
    const triggerSelect = document.getElementById("sched-trigger").value;
    const timeVal = document.getElementById("sched-time-val").value;
    const intervalVal = document.getElementById("sched-interval-val").value;
    
    if (!nameInput) {
        alert("請輸入任務名稱");
        return;
    }
    
    let taskValue = "";
    if (triggerSelect === "time") {
        taskValue = timeVal || "00:00";
    } else {
        taskValue = intervalVal || "60";
        if (parseInt(taskValue) <= 0) {
            alert("間隔時間必須大於 0");
            return;
        }
    }
    
    let enabled = true;
    let lastRun = 0.0;
    if (idInput) {
        const oldTask = currentSchedulerTasks.find(t => t.task_id === idInput);
        if (oldTask) {
            enabled = oldTask.enabled !== undefined ? oldTask.enabled : true;
            lastRun = oldTask.last_run || 0.0;
        }
    }
    
    const taskData = {
        task_id: idInput || null,
        name: nameInput,
        type: typeSelect,
        trigger: triggerSelect,
        value: taskValue,
        param: typeSelect === "command" ? paramInput : "",
        enabled: enabled,
        last_run: lastRun
    };
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/scheduler`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(taskData)
        });
        
        if (res.ok) {
            modalScheduler.classList.add("d-none");
            loadSchedulerTasks();
        } else {
            const err = await res.json();
            alert(`儲存任務失敗: ${err.detail}`);
        }
    } catch(e) {
        alert("網路錯誤");
    }
}

async function toggleTaskEnabled(task) {
    if (!selectedServerId) return;
    const updatedTask = { ...task, enabled: !task.enabled };
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/scheduler`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updatedTask)
        });
        if (res.ok) {
            loadSchedulerTasks();
        }
    } catch(e) {
        console.error("切換任務狀態失敗", e);
    }
}

async function deleteSchedulerTask(taskId) {
    if (!selectedServerId) return;
    if (!confirm("確定要刪除此定時任務嗎？")) return;
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/scheduler/${taskId}`, {
            method: "DELETE"
        });
        if (res.ok) {
            loadSchedulerTasks();
        }
    } catch(e) {
        alert("刪除定時任務失敗");
    }
}

// 3. 高效去重備份與還原系統 (方案 A)
async function loadBackups() {
    if (!selectedServerId) return;
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/backups`);
        if (res.ok) {
            const backups = await res.json();
            renderBackups(backups);
        }
    } catch(e) {
        console.error("載入備份列表失敗", e);
    }
}

// 格式化檔案大小
function formatBytes(bytes) {
    if (bytes === 0) return "0 Bytes";
    const k = 1024;
    const sizes = ["Bytes", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}

function renderBackups(backups) {
    const body = document.getElementById("backup-list-body");
    if (!body) return;
    body.innerHTML = "";
    
    if (backups.length === 0) {
        body.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 30px;">目前尚無歷史備份版本。</td></tr>';
        return;
    }
    
    backups.forEach(backup => {
        const row = document.createElement("tr");
        
        const descCell = document.createElement("td");
        descCell.innerText = backup.description;
        descCell.style.fontWeight = "600";
        
        const countCell = document.createElement("td");
        countCell.innerText = `${backup.file_count} 個檔案`;
        
        const sizeCell = document.createElement("td");
        sizeCell.innerText = formatBytes(backup.total_size);
        
        const timeCell = document.createElement("td");
        timeCell.innerText = new Date(backup.timestamp * 1000).toLocaleString();
        
        const actionCell = document.createElement("td");
        actionCell.className = "file-row-actions";
        
        // 還原按鈕
        const restoreBtn = document.createElement("button");
        restoreBtn.className = "btn btn-success btn-sm";
        restoreBtn.innerHTML = '<i class="fa-solid fa-rotate-left"></i> 還原';
        restoreBtn.addEventListener("click", () => restoreBackup(backup.backup_id));
        actionCell.appendChild(restoreBtn);
        
        // 刪除按鈕
        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-icon btn-sm";
        delBtn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
        delBtn.title = "刪除備份版本";
        delBtn.addEventListener("click", () => deleteBackup(backup.backup_id));
        actionCell.appendChild(delBtn);
        
        row.appendChild(descCell);
        row.appendChild(countCell);
        row.appendChild(sizeCell);
        row.appendChild(timeCell);
        row.appendChild(actionCell);
        body.appendChild(row);
    });
}

async function createBackup() {
    if (!selectedServerId) return;
    const desc = prompt("請輸入此去重備份的備忘描述/備註:");
    if (desc === null) return; // 按取消
    const finalDesc = desc.trim() || `手動備份_${new Date().toLocaleString()}`;
    
    appendSystemLog("[系統提示] 開始執行 Git 式內容定址去重備份，這需要一些時間，請稍候...");
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/backups`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description: finalDesc })
        });
        
        if (res.ok) {
            appendSystemLog("[系統提示] 去重備份執行成功！已完成物件分析與定址保存。");
            loadBackups();
        } else {
            const err = await res.json();
            appendSystemLog(`[系統錯誤] 備份失敗: ${err.detail}`, true);
            alert(`備份失敗: ${err.detail}`);
        }
    } catch(e) {
        appendSystemLog(`[系統錯誤] 網路錯誤，備份連線中斷`, true);
    }
}

async function restoreBackup(backupId) {
    if (!selectedServerId) return;
    if (!confirm("確定要將伺服器還原至此備份版本嗎？\n\n警告：目前伺服器的所有檔案都將被完全覆蓋，且如果伺服器正在運行，它將被自動重啟！此動作無法還原。")) return;
    
    appendSystemLog("[系統提示] 開始執行去重備份還原中，請稍候...");
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/backups/${backupId}/restore`, {
            method: "POST"
        });
        if (res.ok) {
            appendSystemLog("[系統提示] 伺服器還原成功！所有檔案皆已還原至指定版本。");
            alert("伺服器還原成功！");
            loadServerList(); // 重新拉取狀態
            loadFiles(currentPath);
            loadBackups();
        } else {
            const err = await res.json();
            appendSystemLog(`[系統錯誤] 還原失敗: ${err.detail}`, true);
            alert(`還原失敗: ${err.detail}`);
        }
    } catch(e) {
        appendSystemLog(`[系統錯誤] 網路連線錯誤，還原失敗`, true);
    }
}

async function deleteBackup(backupId) {
    if (!selectedServerId) return;
    if (!confirm("確定要刪除此備份版本嗎？這將會清除與其關聯的 manifestation 對照表，並觸發全局垃圾回收 (GC) 清除不再有備份引用的孤兒物件檔案。")) return;
    
    try {
        const res = await fetch(`/api/servers/${selectedServerId}/backups/${backupId}`, {
            method: "DELETE"
        });
        if (res.ok) {
            loadBackups();
        } else {
            const err = await res.json();
            alert(`刪除備份失敗: ${err.detail}`);
        }
    } catch(e) {
        alert("網路錯誤");
    }
}

// 4. 全局系統設定 (Discord 警報與自啟動)
async function loadGlobalConfig() {
    try {
        const res = await fetch("/api/global/config");
        if (res.ok) {
            const config = await res.json();
            sysAutostart.checked = config.autostart !== false;
            sysDiscordEnabled.checked = config.discord_enabled || false;
            sysDiscordToken.value = config.discord_token || "";
            sysDiscordChannel.value = config.discord_channel_id || "";
        }
    } catch(e) {
        console.error("載入全局設定失敗", e);
    }
}

async function saveGlobalConfig() {
    const autostart = sysAutostart.checked;
    const enabled = sysDiscordEnabled.checked;
    const token = sysDiscordToken.value.trim();
    const channel = sysDiscordChannel.value.trim();
    
    try {
        const res = await fetch("/api/global/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                discord_enabled: enabled,
                discord_token: token,
                discord_channel_id: channel,
                autostart: autostart
            })
        });
        if (res.ok) {
            alert("系統全局設定儲存成功！");
        } else {
            const err = await res.json();
            alert(`儲存設定失敗: ${err.detail}`);
        }
    } catch(e) {
        alert("網路錯誤，儲存設定失敗");
    }
}

async function testDiscordAlert() {
    const autostart = sysAutostart.checked;
    const enabled = sysDiscordEnabled.checked;
    const token = sysDiscordToken.value.trim();
    const channel = sysDiscordChannel.value.trim();
    
    if (!token || !channel) {
        alert("請先填寫 Discord Bot Token 與 接收頻道 ID。");
        return;
    }
    
    const originalText = btnTestDiscord.innerHTML;
    btnTestDiscord.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 測試中...';
    btnTestDiscord.disabled = true;
    
    try {
        const res = await fetch("/api/global/config/test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                discord_enabled: enabled,
                discord_token: token,
                discord_channel_id: channel,
                autostart: autostart
            })
        });
        
        if (res.ok) {
            alert("測試訊息已成功發送至您的 Discord 頻道！請前往查看。");
        } else {
            const err = await res.json();
            alert(`測試發送失敗: ${err.detail}`);
        }
    } catch(e) {
        alert("網路連線錯誤，測試發送失敗");
    } finally {
        btnTestDiscord.innerHTML = originalText;
        btnTestDiscord.disabled = false;
    }
}

// 5. 清理已刪除伺服器的殘留備份與垃圾回收 (L-2 擴充)
async function cleanupOrphanBackups() {
    if (!confirm("確定要清理所有已刪除伺服器的殘留備份嗎？此操作將永久刪除這些歷史備份紀錄並執行垃圾回收，無法還原！")) return;
    
    const originalText = btnCleanupBackups.innerHTML;
    btnCleanupBackups.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 清理中...';
    btnCleanupBackups.disabled = true;
    
    try {
        const res = await fetch("/api/global/backups/cleanup", { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            alert(data.message);
        } else {
            const err = await res.json();
            alert(`清理失敗: ${err.detail}`);
        }
    } catch(e) {
        alert("網路錯誤，清理失敗");
    } finally {
        btnCleanupBackups.innerHTML = originalText;
        btnCleanupBackups.disabled = false;
    }
}

