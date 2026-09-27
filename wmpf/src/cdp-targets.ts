// CDP Target Management Module
// Handles listing and attaching to Chrome DevTools Protocol targets

import WebSocket from "ws";

const DEBUG = false;

export interface CDPTarget {
    targetId: string;
    type: string;
    title: string;
    url: string;
    attached: boolean;
    canAccessOpener?: boolean;
}

export interface CDPMessage {
    id?: number;
    method?: string;
    params?: any;
    result?: any;
    error?: any;
}

export class CDPClient {
    private ws: WebSocket | null = null;
    private messageId = 0;
    private pendingRequests: Map<number, { resolve: (value: any) => void; reject: (error: any) => void }> = new Map();

    constructor(private url: string = "ws://127.0.0.1:62000") {}

    async connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            this.ws = new WebSocket(this.url);

            this.ws.on("open", () => {
                console.log("[cdp] connected to CDP endpoint");
                resolve();
            });

            this.ws.on("error", (err) => {
                console.error("[cdp] connection error:", err);
                reject(err);
            });

            this.ws.on("message", (data: string) => {
                try {
                    const message: CDPMessage = JSON.parse(data);
                    DEBUG && console.log("[cdp] received:", message);

                    if (message.id !== undefined && this.pendingRequests.has(message.id)) {
                        const { resolve, reject } = this.pendingRequests.get(message.id)!;
                        this.pendingRequests.delete(message.id);

                        if (message.error) {
                            reject(message.error);
                        } else {
                            resolve(message.result);
                        }
                    }
                } catch (e) {
                    console.error("[cdp] failed to parse message:", e);
                }
            });

            this.ws.on("close", () => {
                console.log("[cdp] connection closed");
                // Reject all pending requests
                this.pendingRequests.forEach(({ reject }) => {
                    reject(new Error("Connection closed"));
                });
                this.pendingRequests.clear();
            });
        });
    }

    async send(method: string, params: any = {}): Promise<any> {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            throw new Error("CDP client not connected");
        }

        const id = ++this.messageId;
        const message: CDPMessage = { id, method, params };

        return new Promise((resolve, reject) => {
            this.pendingRequests.set(id, { resolve, reject });
            this.ws!.send(JSON.stringify(message), (err) => {
                if (err) {
                    this.pendingRequests.delete(id);
                    reject(err);
                }
            });
        });
    }

    async getTargets(): Promise<CDPTarget[]> {
        try {
            const result = await this.send("Target.getTargets");
            return result.targetInfos || [];
        } catch (e) {
            console.error("[cdp] failed to get targets:", e);
            return [];
        }
    }

    async attachToTarget(targetId: string): Promise<boolean> {
        try {
            await this.send("Target.attachToTarget", {
                targetId,
                flatten: true
            });
            console.log(`[cdp] attached to target: ${targetId}`);
            return true;
        } catch (e) {
            console.error(`[cdp] failed to attach to target ${targetId}:`, e);
            return false;
        }
    }

    async detachFromTarget(targetId: string): Promise<boolean> {
        try {
            await this.send("Target.detachFromTarget", { targetId });
            console.log(`[cdp] detached from target: ${targetId}`);
            return true;
        } catch (e) {
            console.error(`[cdp] failed to detach from target ${targetId}:`, e);
            return false;
        }
    }

    close(): void {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }
}

// Format target list for display
export function formatTargetList(targets: CDPTarget[]): string {
    if (targets.length === 0) {
        return "No CDP targets found. Make sure WeChat is running and a miniapp/page is open.";
    }

    let output = "\nAvailable CDP Targets:\n";
    output += "─────────────────────────────────────────────────────────────\n";

    targets.forEach((target, index) => {
        const status = target.attached ? "[ATTACHED]" : "[AVAILABLE]";
        output += `${index + 1}. ${status} ${target.type}\n`;
        output += `   Title: ${target.title.substring(0, 60)}${target.title.length > 60 ? "..." : ""}\n`;
        output += `   URL: ${target.url.substring(0, 60)}${target.url.length > 60 ? "..." : ""}\n`;
        output += `   TargetID: ${target.targetId}\n`;
        output += "\n";
    });

    output += "─────────────────────────────────────────────────────────────\n";
    output += `Total: ${targets.length} target(s)\n`;
    output += "\nTo attach to a target, use the attach() method with the targetId.\n";

    return output;
}

// Filter targets by type
export function filterTargetsByType(targets: CDPTarget[], type: string): CDPTarget[] {
    return targets.filter(t => t.type.toLowerCase() === type.toLowerCase());
}

// Filter targets by title pattern
export function filterTargetsByTitle(targets: CDPTarget[], pattern: string): CDPTarget[] {
    const regex = new RegExp(pattern, "i");
    return targets.filter(t => regex.test(t.title));
}

// Find miniapp targets
export function findMiniAppTargets(targets: CDPTarget[]): CDPTarget[] {
    return targets.filter(t =>
        t.type === "page" ||
        t.type === "webview" ||
        t.title.includes("小程序") ||
        t.url.includes("servicewechat.com")
    );
}

// Find browser targets (articles, videos, etc.)
export function findBrowserTargets(targets: CDPTarget[]): CDPTarget[] {
    return targets.filter(t =>
        t.type === "page" &&
        !t.title.includes("小程序") &&
        !t.url.includes("servicewechat.com")
    );
}
