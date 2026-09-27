import { promises } from "node:fs";
import { EventEmitter } from "node:events";
import path from "node:path";
import { AddressInfo } from "node:net";
import { execSync } from "node:child_process";
import * as frida from "frida";
import WebSocket, { WebSocketServer } from "ws";

const codex = require("./third-party/RemoteDebugCodex.js");
const messageProto = require("./third-party/WARemoteDebugProtobuf.js");

class DebugMessageEmitter extends EventEmitter {}

const DEBUG_PORT = 9421;
const CDP_PORT = 62000;
const DEBUG = false;

const debugMessageEmitter = new DebugMessageEmitter();
let messageCounter = 0;
let sessionCounter = 0;
let latestMiniappWs: WebSocket | null = null;

const bufferToHexString = (buffer: ArrayBuffer) => {
    return Array.from(new Uint8Array(buffer)).map(byte => byte.toString(16).padStart(2, '0')).join("");
}

const wrapAndSend = (ws: WebSocket, payload: string) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const rawPayload = {
        jscontext_id: "",
        op_id: Math.round(100 * Math.random()),
        payload: payload
    };
    DEBUG && console.log(rawPayload);
    const wrappedData = codex.wrapDebugMessageData(rawPayload, "chromeDevtools", 0);
    const outData = {
        seq: ++messageCounter,
        category: "chromeDevtools",
        data: wrappedData.buffer,
        compressAlgo: 0,
        originalSize: wrappedData.originalSize
    }
    const encoded = messageProto.mmbizwxadevremote.WARemoteDebug_DebugMessage.encode(outData).finish();
    ws.send(encoded, { binary: true });
}

const debug_server = () => {
    const wss = new WebSocketServer({ port: DEBUG_PORT });
    console.log(`[server] debug server running on ws://localhost:${DEBUG_PORT}`);
    console.log(`[server] debug server waiting for miniapp to connect`);

    wss.on("connection", (miniappWs: WebSocket) => {
        const sessionId = ++sessionCounter;

        const sessionProxy = new WebSocketServer({ port: 0, host: "127.0.0.1" });

        sessionProxy.on("listening", () => {
            const addr = sessionProxy.address() as AddressInfo;
            console.log(`[miniapp] miniapp client connected, session: ${sessionId}, cdp port: ${addr.port}`);
        });

        miniappWs.on("message", (message: ArrayBuffer) => {
            DEBUG && console.log(`[miniapp] received raw message (hex): ${bufferToHexString(message)}`);
            let unwrappedData: any = null;
            try {
                const decodedData = messageProto.mmbizwxadevremote.WARemoteDebug_DebugMessage.decode(message);
                unwrappedData = codex.unwrapDebugMessageData(decodedData);
                DEBUG && console.log(`[miniapp] [DEBUG] decoded data:`);
                DEBUG && console.dir(unwrappedData);
            } catch (e) {
                console.error(`[miniapp] miniapp client err: ${e}`);
            }

            if (unwrappedData === null) {
                return;
            }

            if (unwrappedData.category === "chromeDevtoolsResult") {
                const payload = unwrappedData.data.payload;
                sessionProxy.clients.forEach(c => {
                    if (c.readyState === WebSocket.OPEN) c.send(payload);
                });
                if (miniappWs === latestMiniappWs) {
                    debugMessageEmitter.emit("cdpmessage", payload);
                }
            }
        });

        sessionProxy.on("connection", (dtWs: WebSocket) => {
            console.log(`[miniapp] session ${sessionId} DevTools connected`);
            dtWs.on("message", (msg) => {
                wrapAndSend(miniappWs, msg.toString());
            });
        });

        miniappWs.on("error", (err) => {
            console.error(`[miniapp] miniapp client err:`, err);
        });
        miniappWs.on("close", () => {
            console.log(`[miniapp] miniapp client disconnected, session: ${sessionId}`);
            sessionProxy.close();
            if (miniappWs === latestMiniappWs) {
                latestMiniappWs = null;
            }
        });

        latestMiniappWs = miniappWs;
    });

    debugMessageEmitter.on("proxymessage", (message: string) => {
        if (latestMiniappWs && latestMiniappWs.readyState === WebSocket.OPEN) {
            wrapAndSend(latestMiniappWs, message);
        }
    });
}

const proxy_server = () => {
    const wss = new WebSocketServer({ port: CDP_PORT });
    console.log(`[server] proxy server running on ws://localhost:${CDP_PORT}`);
    console.log(`[server] link: devtools://devtools/bundled/inspector.html?ws=127.0.0.1:${CDP_PORT}`);

    wss.on("connection", (ws: WebSocket) => {
        console.log("[cdp] CDP client connected");
        ws.on("message", (message: ArrayBuffer | string, isBinary: boolean) => {
            const msg = isBinary ? Buffer.from(message as ArrayBuffer).toString() : message.toString();
            debugMessageEmitter.emit("proxymessage", msg);
        });
        ws.on("error", (err) => { console.error("[cdp] CDP client err:", err) });
        ws.on("close", () => { console.log("[cdp] CDP client disconnected") });
    });

    debugMessageEmitter.on("cdpmessage", (message: string) => {
        wss.clients.forEach(client => {
            if (client.readyState === WebSocket.OPEN) {
                client.send(message);
            }
        });
    });
}

const getProjectRoot = () => {
    return path.join(
        path.dirname(
            (require.main && require.main.filename) ||
            (process.mainModule && process.mainModule.filename) ||
            process.cwd()
        ), ".."
    );
};

const getWeChatAppExPath = (pid: number): string | null => {
    try {
        const output = execSync(
            `powershell -NoProfile -Command "(Get-Process -Id ${pid}).Path"`,
            { encoding: "utf8", timeout: 10000 }
        );
        const trimmed = output.trim();
        return trimmed.length > 0 ? trimmed : null;
    } catch (e) {
        return null;
    }
};

const frida_server = async () => {
    const localDevice = await frida.getLocalDevice();
    const processes = await localDevice.enumerateProcesses();
    const wmpfProcesses = processes.filter(process => process.name === "WeChatAppEx.exe");

    if (wmpfProcesses.length === 0) {
        throw new Error("[frida] WeChatAppEx.exe process not found");
    }

    const wmpfProcess = wmpfProcesses[0];
    const wmpfPid = wmpfProcess.pid;
    const wmpfProcessPath = getWeChatAppExPath(wmpfPid);
    const wmpfVersionMatch = wmpfProcessPath ? wmpfProcessPath.match(/\d+/g) : "";
    const wmpfVersion = wmpfVersionMatch ? new Number(wmpfVersionMatch.pop()) : 0;
    if (wmpfVersion === 0) {
        throw new Error("[frida] error in find wmpf version");
        return;
    }

    const session = await localDevice.attach(Number(wmpfPid));

    const projectRoot = getProjectRoot();
    let scriptContent: string | null = null;
    try {
        scriptContent = (await promises.readFile(path.join(projectRoot, "frida/hook.js"))).toString();
    } catch (e) {
        throw new Error("[frida] hook script not found");
        return;
    }

    let configContent: string | null = null;
    try {
        configContent = (await promises.readFile(path.join(projectRoot, "frida/config", `addresses.${wmpfVersion}.json`))).toString();
        configContent = JSON.stringify(JSON.parse(configContent));
    } catch (e) {
        throw new Error(`[frida] version config not found: ${wmpfVersion}`);
    }

    if (scriptContent === null || configContent === null) {
        throw new Error("[frida] unable to find hook script");
        return;
    }

    const script = await session.createScript(scriptContent.replace("@@CONFIG@@", configContent));
    script.message.connect((message: frida.Message) => {
        if (message.type === "error") {
            console.error("[frida client]", message);
            return;
        }
        console.log("[frida client]", message.payload);
    });
    await script.load();
    console.log(`[frida] script loaded, WMPF version: ${wmpfVersion}, pid: ${wmpfPid}`);
    console.log(`[frida] you can now open any miniapps`);
}

const main = async () => {
    console.log("[server] 启动调试服务");
    debug_server();
    proxy_server();
    frida_server().catch(e => {
        console.error(e.message);
    });
}

(async () => {
    await main();
})();
