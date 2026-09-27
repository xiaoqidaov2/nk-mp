# -*- coding: utf-8 -*-
"""Frida 注入器: 把 25715 定制 hook 注入微信小程序主进程(浏览器进程)."""
import sys, time, frida, subprocess

def find_main_appex():
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name='WeChatAppEx.exe'\" | "
         "Select-Object ProcessId, CommandLine | ConvertTo-Json"],
        capture_output=True, text=True, timeout=30)
    import json
    data = json.loads(out.stdout)
    procs = data if isinstance(data, list) else [data]
    for p in procs:
        cmd = p.get("CommandLine", "") or ""
        if "--type=" not in cmd:
            return p["ProcessId"], cmd[:120]
    return None, None

def on_message(message, data):
    if message["type"] == "send":
        print(f"[hook] {message['payload']}")
    elif message["type"] == "error":
        print(f"[hook-ERR] {message.get('description', message)}")

def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not pid:
        found = find_main_appex()
        if not found:
            print("未找到主 WeChatAppEx 进程（小程序未打开?）")
            return
        pid, cmd = found
        print(f"[inject] 主进程 pid={pid}")
    else:
        print(f"[inject] 指定 pid={pid}")

    session = frida.attach(pid)
    script_src = open(r"C:\Users\qq289\Documents\nike_mp\wmpf\hook_25715.js", encoding="utf-8").read()
    script = session.create_script(script_src)
    script.on("message", on_message)
    script.load()
    print("[inject] hook 已注入并加载, 现在打开/刷新小程序")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
