# -*- coding: utf-8 -*-
"""进程监视注入器: 轮询新出现的 WeChatAppEx 主进程, 立即注入 hook (冷启动竞速)."""
import subprocess, json, time, sys, frida

HOOK = r"C:\Users\qq289\Documents\nike_mp\wmpf\hook_25715.js"
injected = set()

def list_main_pids():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name='WeChatAppEx.exe'\" | "
         "Select-Object ProcessId, CommandLine | ConvertTo-Json"],
        capture_output=True, text=True, timeout=20)
    try:
        data = json.loads(out.stdout)
    except Exception:
        return []
    procs = data if isinstance(data, list) else [data]
    return [p["ProcessId"] for p in procs if "--type=" not in (p.get("CommandLine") or "")]

def on_message(message, data):
    if message["type"] == "send":
        print(f"[hook] {message['payload']}", flush=True)
    elif message["type"] == "error":
        print(f"[hook-ERR] {message.get('description', message)}", flush=True)

def main():
    src = open(HOOK, encoding="utf-8").read()
    print("[watch] 监视中, 等待新的 WeChatAppEx 主进程...", flush=True)
    end = time.time() + 1800  # 最多监视 5 分钟
    while time.time() < end:
        for pid in list_main_pids():
            if pid in injected:
                continue
            try:
                session = frida.attach(pid)
                script = session.create_script(src)
                script.on("message", on_message)
                script.load()
                injected.add(pid)
                print(f"[watch] 已注入 pid={pid}", flush=True)
            except Exception as e:
                print(f"[watch] pid={pid} 注入失败: {str(e)[:80]}", flush=True)
        time.sleep(0.2)
    print("[watch] 退出", flush=True)

if __name__ == "__main__":
    main()
