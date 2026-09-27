// 独立版调试桥: 9421 = 微信小程序调试协议入口, 6200 = CDP 代理
const { EventEmitter } = require("events");
const WebSocket = require("ws");
const WebSocketServer = WebSocket.WebSocketServer || WebSocket.Server;
const codex = require("./RemoteDebugCodex.js");
const messageProto = require("./WARemoteDebugProtobuf.js");

const DEBUG_PORT = 9421, CDP_PORT = 62000, DEBUG = false;
const emitter = new EventEmitter();
let counter = 0, latest = null;

const wrapAndSend = (ws, payload) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const raw = { jscontext_id: "", op_id: Math.round(100 * Math.random()), payload };
    const wrapped = codex.wrapDebugMessageData(raw, "chromeDevtools", 0);
    const encoded = messageProto.mmbizwxadevremote.WARemoteDebug_DebugMessage.encode({
        seq: ++counter, category: "chromeDevtools", data: wrapped.buffer,
        compressAlgo: 0, originalSize: wrapped.originalSize }).finish();
    ws.send(encoded, { binary: true });
};

const debug_server = () => {
    const wss = new WebSocketServer({ port: DEBUG_PORT });
    console.log(`[bridge] listening ws://127.0.0.1:${DEBUG_PORT}`);
    wss.on("connection", (miniWs) => {
        const proxy = new WebSocketServer({ port: 0, host: "127.0.0.1" });
        proxy.on("listening", () => {
            const a = proxy.address();
            console.log(`[bridge] miniapp connected! cdp port: ${a.port}`);
        });
        miniWs.on("message", (message) => {
            let ud = null;
            try {
                const d = messageProto.mmbizwxadevremote.WARemoteDebug_DebugMessage.decode(message);
                ud = codex.unwrapDebugMessageData(d);
            } catch (e) { console.error(`[bridge] decode err: ${e}`); }
            if (ud === null) return;
            if (ud.category === "chromeDevtoolsResult") {
                proxy.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(ud.data.payload); });
                emitter.emit("cdpmessage", ud.data.payload);
            }
        });
        miniWs.on("error", (e) => console.error("[bridge] ws err:", e));
        miniWs.on("close", () => { console.log("[bridge] miniapp disconnected"); proxy.close(); });
        latest = miniWs;
    });
    emitter.on("proxymessage", (m) => {
        if (latest && latest.readyState === WebSocket.OPEN) wrapAndSend(latest, m);
    });
};

const proxy_server = () => {
    const wss = new WebSocketServer({ port: CDP_PORT });
    console.log(`[bridge] CDP on ws://127.0.0.1:${CDP_PORT}`);
    wss.on("connection", (ws) => {
        console.log("[cdp] client connected");
        ws.on("message", (msg, isBinary) => {
            const s = isBinary ? Buffer.from(msg).toString() : msg.toString();
            emitter.emit("proxymessage", s);
        });
        ws.on("error", (e) => console.error("[cdp] err:", e));
    });
    emitter.on("cdpmessage", (m) => {
        wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(m); });
    });
};

debug_server(); proxy_server();
