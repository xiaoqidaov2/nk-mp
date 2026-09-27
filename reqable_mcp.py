"""Reqable MCP stdio 直连客户端：每次调用重新握手，批量执行操作后退出。"""
import subprocess, json, threading, queue, time, sys

EXE = r"C:\Program Files\Reqable\mcp-server.exe"

class Reqable:
    def __init__(self, timeout=20):
        self.proc = subprocess.Popen([EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace")
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc.stdout,), daemon=True).start()
        self.timeout = timeout
        self._id = 0
        self.init = self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "zcode", "version": "1.0"}})
        self._notify("notifications/initialized")

    def _reader(self, stream):
        for line in stream:
            line = line.strip()
            if line:
                self.q.put(line)

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _rpc(self, method, params=None, timeout=None):
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error']}")
                return msg.get("result")
        raise TimeoutError(f"{method} 无响应")

    def tools(self):
        return self._rpc("tools/list")["tools"]

    def schema(self, name):
        for t in self.tools():
            if t["name"] == name:
                return t
        raise KeyError(name)

    def call(self, name, args=None, timeout=None):
        r = self._rpc("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout or 60)
        content = r.get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return {"isError": r.get("isError", False), "text": text, "raw": r}

    def close(self):
        try: self.proc.kill()
        except Exception: pass
