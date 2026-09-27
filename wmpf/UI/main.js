const { app, BrowserWindow, Menu, ipcMain } = require('electron');
const { spawn, execSync } = require('child_process');
const path = require('path');

if (process.platform === 'win32') {
    if (process.stdin && typeof process.stdin.setEncoding === 'function') process.stdin.setEncoding('utf8');
    if (process.stdout && typeof process.stdout.setEncoding === 'function') process.stdout.setEncoding('utf8');
    if (process.stderr && typeof process.stderr.setEncoding === 'function') process.stderr.setEncoding('utf8');
}

let mainWindow;
let devToolsWindow;
let serverProcess;
let isServerRunning = false;
let isMonitoringMiniProgram = false;
let cdpMessageId = 0;
let monitorInterval = null;
let monitoringStarted = false;
let hasOpenedDevTools = false;
let hasSentInitialLog = false;
let lastProcessCount = -1;
let initialWeChatProcesses = [];
let activeWeChatPids = [];
let fridaReady = false;
let pendingOpenTimer = null;
let pendingCloseTimer = null;
const targetProxies = new Map();

function safeSend(channel, data) {
    try {
        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send(channel, data);
        }
    } catch (e) {}
}

function sendLog(type, message) {
    safeSend('log-message', {
        type: type,
        message: message,
        timestamp: new Date().toLocaleTimeString()
    });
    console[type === 'error' ? 'error' : 'log'](message);
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 960,
        height: 720,
        minWidth: 800,
        minHeight: 600,
        title: 'WMPF Debugger',
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false,
            webSecurity: false,
        }
    });

    mainWindow.loadFile(path.join(__dirname, 'app.html'));
    mainWindow.on('closed', function () {
        mainWindow = null;
        stopServer();
    });
}

function startServer() {
    const serverPath = path.join(__dirname, '..', 'dist', 'index.js');
    serverProcess = spawn('node', [serverPath, 'miniapp'], {
        cwd: path.join(__dirname, '..'),
        stdio: ['ignore', 'pipe', 'pipe']
    });

    let pendingOpenTimer = null;
    let pendingCloseTimer = null;

    serverProcess.stdout.on('data', (data) => {
        const output = data.toString().trim();
        if (!output) return;
        sendLog('info', output);

        if (output.includes('[frida] script loaded')) {
            fridaReady = true;
            sendLog('info', 'Frida 已就绪，等待小程序连接');
        }

        if (output.includes('[miniapp] miniapp client connected')) {
            if (pendingCloseTimer) { clearTimeout(pendingCloseTimer); pendingCloseTimer = null; }
            if (devToolsWindow || !fridaReady || !monitoringStarted) return;

            if (pendingOpenTimer) { clearTimeout(pendingOpenTimer); }
            sendLog('info', '检测到小程序连接，等待稳定');
            pendingOpenTimer = setTimeout(() => {
                pendingOpenTimer = null;
                if (!isServerRunning || devToolsWindow || hasOpenedDevTools) return;
                tryOpenMiniProgramDevTools();
            }, 300);
        }

        if (output.includes('[miniapp] miniapp client disconnected')) {
            if (pendingOpenTimer) { clearTimeout(pendingOpenTimer); pendingOpenTimer = null; }
            if (!devToolsWindow) return;

            if (pendingCloseTimer) { clearTimeout(pendingCloseTimer); }
            sendLog('warning', '小程序连接断开，等待确认');
            pendingCloseTimer = setTimeout(() => {
                pendingCloseTimer = null;
                sendLog('warning', '小程序已断开连接');
                closeMiniProgramDevTools();
            }, 300);
        }
    });

    serverProcess.stderr.on('data', (data) => {
        const output = data.toString().trim();
        if (output) sendLog('error', output);
    });

    serverProcess.on('spawn', () => {
        isServerRunning = true;
        safeSend('server-status', { running: true });
        sendLog('success', '调试器已启动');
        sendLog('info', '正在监控 WeChatAppEx 进程');
        sendLog('info', '先打开小程序建立调试会话，然后点"目标列表"可调试浏览器页面');
        startMiniProgramMonitoring();
    });

    serverProcess.on('close', (code) => {
        isServerRunning = false;
        stopMiniProgramMonitoring();
        safeSend('server-status', { running: false });
        sendLog('info', '服务已退出');
        if (devToolsWindow && !devToolsWindow.isDestroyed()) devToolsWindow.close();
        devToolsWindow = null;
        closeAllTargetProxies();
    });

    serverProcess.on('error', (err) => {
        isServerRunning = false;
        stopMiniProgramMonitoring();
        safeSend('server-status', { running: false });
        sendLog('error', '启动失败: ' + err.message);
    });
}

function killServerProcess() {
    if (!serverProcess) return;
    const pid = serverProcess.pid;
    try {
        if (process.platform === 'win32' && pid) {
            execSync(`taskkill /F /T /PID ${pid}`, { timeout: 5000, stdio: 'ignore' });
        } else {
            serverProcess.kill('SIGKILL');
        }
    } catch (e) {
        try { serverProcess.kill(); } catch (e2) {}
    }
    serverProcess = null;
}

function stopServer() {
    killServerProcess();
    isServerRunning = false;
    stopMiniProgramMonitoring();
    fridaReady = false;
    safeSend('server-status', { running: false });
    closeMiniProgramDevTools();
    closeAllTargetProxies();
}

function closeAllTargetProxies() {
    for (const [targetId, proxy] of targetProxies) {
        try {
            if (proxy.upstream) proxy.upstream.close();
            if (proxy.downstream) proxy.downstream.close();
            if (proxy.window && !proxy.window.isDestroyed()) proxy.window.close();
        } catch (e) {}
    }
    targetProxies.clear();
}

function openTargetDevTools(targetId, targetTitle) {
    if (targetProxies.has(targetId)) {
        const existing = targetProxies.get(targetId);
        if (existing.window && !existing.window.isDestroyed()) {
            existing.window.focus();
        }
        return;
    }

    const WebSocket = require('ws');

    const downstream = new WebSocket.Server({ port: 0, host: '127.0.0.1' });

    downstream.on('listening', () => {
        const downstreamPort = downstream.address().port;

        const upstream = new WebSocket('ws://127.0.0.1:62000');
        let sessionId = null;
        const attachId = ++cdpMessageId;

        const proxy = { upstream, downstream, sessionId: null, window: null };
        targetProxies.set(targetId, proxy);

        upstream.on('open', () => {
            upstream.send(JSON.stringify({
                id: attachId,
                method: 'Target.attachToTarget',
                params: { targetId: targetId, flatten: true }
            }));
        });

        upstream.on('message', (data) => {
            let msg;
            try { msg = JSON.parse(data.toString()); } catch (e) { return; }

            if (msg.id === attachId && msg.result && msg.result.sessionId && !sessionId) {
                sessionId = msg.result.sessionId;
                proxy.sessionId = sessionId;
                sendLog('success', '已附加到目标: ' + (targetTitle || targetId.substring(0, 12)));

                let dtw = new BrowserWindow({
                    width: 1200,
                    height: 800,
                    title: '网页调试 - ' + (targetTitle || targetId.substring(0, 12)),
                    webPreferences: {
                        nodeIntegration: false,
                        contextIsolation: true,
                        webSecurity: false,
                    }
                });
                dtw.loadURL('devtools://devtools/bundled/inspector.html?ws=127.0.0.1:' + downstreamPort);
                dtw.on('closed', () => {
                    try { upstream.close(); } catch (e) {}
                    try { downstream.close(); } catch (e) {}
                    targetProxies.delete(targetId);
                    sendLog('info', '目标 DevTools 已关闭: ' + (targetTitle || targetId.substring(0, 12)));
                });
                proxy.window = dtw;
                return;
            }

            if (msg.id === attachId && msg.error) {
                sendLog('error', '附加失败: ' + JSON.stringify(msg.error));
                upstream.close();
                downstream.close();
                targetProxies.delete(targetId);
                return;
            }

            if (sessionId && msg.sessionId === sessionId) {
                const clean = Object.assign({}, msg);
                delete clean.sessionId;
                try {
                    const cleanStr = JSON.stringify(clean);
                    downstream.clients.forEach(c => {
                        if (c.readyState === WebSocket.OPEN) {
                            c.send(cleanStr);
                        }
                    });
                } catch (e) {}
            }
        });

        downstream.on('connection', (ws) => {
            ws.on('message', (msg) => {
                if (!sessionId) return;
                try {
                    const parsed = JSON.parse(msg.toString());
                    parsed.sessionId = sessionId;
                    upstream.send(JSON.stringify(parsed));
                } catch (e) {}
            });
        });

        upstream.on('error', (err) => {
            sendLog('error', '目标代理连接失败: ' + err.message);
            targetProxies.delete(targetId);
        });

        upstream.on('close', () => {
            try { downstream.close(); } catch (e) {}
            targetProxies.delete(targetId);
        });
    });
}

ipcMain.on('start-server', () => {
    if (isServerRunning) stopServer();
    startServer();
});

ipcMain.on('stop-server', () => {
    stopServer();
});

ipcMain.on('open-devtools', () => {
    if (!isServerRunning) return;
    if (devToolsWindow) { devToolsWindow.focus(); return; }

    devToolsWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        title: '小程序调试 - DevTools',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            webSecurity: false,
        }
    });
    devToolsWindow.loadURL('devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000');
    devToolsWindow.on('closed', function () {
        devToolsWindow = null;
        hasOpenedDevTools = false;
    });
});

ipcMain.on('refresh-devtools', () => {
    if (devToolsWindow && !devToolsWindow.isDestroyed()) devToolsWindow.reload();
});

ipcMain.on('list-targets', async () => {
    if (!isServerRunning) return;
    try {
        const WebSocket = require('ws');
        const ws = new WebSocket('ws://127.0.0.1:62000');
        ws.on('open', () => {
            ws.send(JSON.stringify({ id: 1, method: 'Target.getTargets', params: {} }));
        });
        ws.on('message', (data) => {
            try {
                const response = JSON.parse(data);
                if (response.result && response.result.targetInfos) {
                    safeSend('targets-list', { targets: response.result.targetInfos });
                }
            } catch (e) {}
            ws.close();
        });
        ws.on('error', (err) => {
            safeSend('log-message', {
                type: 'error',
                message: '获取目标列表失败: ' + err.message,
                timestamp: new Date().toLocaleTimeString()
            });
        });
    } catch (e) {
        safeSend('log-message', {
            type: 'error',
            message: '获取目标列表失败: ' + e.message,
            timestamp: new Date().toLocaleTimeString()
        });
    }
});

ipcMain.on('attach-to-target', (event, targetId, targetTitle) => {
    if (!isServerRunning) return;
    openTargetDevTools(targetId, targetTitle);
});

function openDevToolsAutomatically() {
    if (!isServerRunning || !monitoringStarted || devToolsWindow) return;
    devToolsWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        title: '小程序调试 - DevTools',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            webSecurity: false,
        }
    });
    devToolsWindow.loadURL('devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000');
    devToolsWindow.on('closed', function () {
        devToolsWindow = null;
        hasOpenedDevTools = false;
    });
}

function closeMiniProgramDevTools() {
    if (devToolsWindow && !devToolsWindow.isDestroyed()) {
        try { devToolsWindow.close(); } catch (e) {}
    }
    devToolsWindow = null;
    hasOpenedDevTools = false;
}

function tryOpenMiniProgramDevTools() {
    if (!isServerRunning || devToolsWindow || hasOpenedDevTools) return;
    if (!fridaReady) return;

    const WebSocket = require('ws');
    const ws = new WebSocket('ws://127.0.0.1:62000');
    const verifyTimeout = setTimeout(() => {
        try { ws.close(); } catch (e) {}
        sendLog('warning', 'CDP 连接验证超时，将在下次连接时重试');
        hasOpenedDevTools = false;
    }, 5000);

    ws.on('open', () => {
        clearTimeout(verifyTimeout);
        ws.close();
        sendLog('success', '验证通过，正在打开小程序调试控制台');
        hasOpenedDevTools = true;
        openDevToolsAutomatically();
    });
    ws.on('error', () => {
        clearTimeout(verifyTimeout);
        sendLog('warning', 'CDP 服务尚未就绪，将在下次连接时重试');
        hasOpenedDevTools = false;
    });
}

function getWeChatAppExPids() {
    try {
        const output = execSync('tasklist /FI "IMAGENAME eq WeChatAppEx.exe" /FO CSV /NH', {
            encoding: 'utf8', timeout: 5000
        });
        const pids = [];
        for (const line of output.trim().split('\n')) {
            const match = line.match(/"WeChatAppEx\.exe","(\d+)"/);
            if (match) pids.push(match[1]);
        }
        return pids;
    } catch (e) {
        return [];
    }
}

function startMiniProgramMonitoring() {
    if (isMonitoringMiniProgram) return;
    isMonitoringMiniProgram = true;
    monitoringStarted = false;
    fridaReady = false;

    setTimeout(() => {
        initialWeChatProcesses = getWeChatAppExPids();
        activeWeChatPids = [...initialWeChatProcesses];
        monitoringStarted = true;
        if (!hasSentInitialLog) {
            sendLog('success', '已记录 ' + initialWeChatProcesses.length + ' 个 WeChatAppEx 进程');
            hasSentInitialLog = true;
        }
    }, 2000);

    monitorInterval = setInterval(() => {
        if (!isServerRunning) {
            clearInterval(monitorInterval);
            monitorInterval = null;
            isMonitoringMiniProgram = false;
            monitoringStarted = false;
            return;
        }
        if (!monitoringStarted) return;
        try {
            const currentPids = getWeChatAppExPids();
            const exitedPids = activeWeChatPids.filter(pid => !currentPids.includes(pid));
            const newPids = currentPids.filter(pid => !activeWeChatPids.includes(pid));

            if (exitedPids.length > 0) {
                sendLog('warning', '检测到 ' + exitedPids.length + ' 个 WeChatAppEx 进程已退出');
                activeWeChatPids = activeWeChatPids.filter(pid => !exitedPids.includes(pid));
                closeMiniProgramDevTools();
            }

            if (newPids.length > 0) {
                activeWeChatPids = [...activeWeChatPids, ...newPids];
                sendLog('info', '检测到 ' + newPids.length + ' 个新 WeChatAppEx 进程');
                lastProcessCount = currentPids.length;
            }
        } catch (e) {}
    }, 3000);
}

function stopMiniProgramMonitoring() {
    if (monitorInterval) { clearInterval(monitorInterval); monitorInterval = null; }
    isMonitoringMiniProgram = false;
    monitoringStarted = false;
    activeWeChatPids = [];
}

app.on('ready', () => { createWindow(); });

app.on('window-all-closed', function () {
    stopServer();
    if (process.platform !== 'darwin') {
        setTimeout(() => { process.exit(0); }, 1500);
        app.quit();
    }
});

app.on('activate', function () {
    if (mainWindow === null) createWindow();
});
