import struct, os, sys, pathlib

def unpack(wxapkg_path, out_dir):
    data = open(wxapkg_path, "rb").read()
    if data[0] != 0xBE:
        print(f"SKIP {wxapkg_path}: magic {data[0]:#x}")
        return 0
    ver, idx_len, body_len = struct.unpack(">III", data[1:13])
    assert data[13] == 0xED, "bad lastMarker"
    file_count = struct.unpack(">I", data[14:18])[0]
    pos = 18
    entries = []
    for _ in range(file_count):
        name_off, offset, size = struct.unpack(">III", data[pos:pos+12])
        pos += 12
        entries.append((name_off, offset, size))
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    n = 0
    for name_off, offset, size in entries:
        name = data[name_off:name_off + 512].split(b"\x00")[0].decode("utf-8", "replace")
        name = name.replace("\\", "/").lstrip("/")
        dest = out / name
        if name.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True); continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[offset:offset+size])
        n += 1
    return n

base = r"C:\Users\qq289\AppData\Roaming\Tencent\xwechat\radium\users\5645cefdbdd3b13181e19923ec200ab9\applet\packages\wx74b301953dcdd7db\18"
outroot = r"C:\Users\qq289\Documents\nike_mp\unpacked"
for fn in os.listdir(base):
    if fn.endswith(".wxapkg"):
        dest = os.path.join(outroot, fn[:-7])
        n = unpack(os.path.join(base, fn), dest)
        print(f"{fn}: {n} files -> {dest}")
