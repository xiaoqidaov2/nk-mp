import hashlib, os, struct, pathlib
from Crypto.Cipher import AES

SALT = b"saltiest"
IV = b"the iv: 16 bytes"
XOR = 0x64
APPID = "wx74b301953dcdd7db"

def decrypt_v1mmwx(data):
    assert data[:6] == b"V1MMWX", "bad magic"
    key = hashlib.pbkdf2_hmac("sha1", APPID.encode(), SALT, 1000, 32)
    cipher = AES.new(key, AES.MODE_CBC, IV)
    head = cipher.decrypt(data[6:6 + 1024])
    rest = bytes(b ^ XOR for b in data[6 + 1024:])
    return head + rest

def unpack_wxapkg(data, out_dir):
    assert data[0] == 0xBE, f"magic {data[0]:#x}"
    ver, idx_len, body_len = struct.unpack(">III", data[1:13])
    assert data[13] == 0xED, f"marker {data[13]:#x}"
    file_count = struct.unpack(">I", data[14:18])[0]
    pos = 18
    entries = []
    for _ in range(file_count):
        if pos + 4 > len(data): break
        name_len = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        name = data[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        if pos + 8 > len(data): break
        offset, size = struct.unpack(">II", data[pos:pos + 8])
        pos += 8
        entries.append((name, offset, size))
    out = pathlib.Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, offset, size in entries:
        name = name.replace("\\", "/").lstrip("/")
        if not name: continue
        dest = out / name
        if name.endswith("/"):
            dest.mkdir(parents=True, exist_ok=True); continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[offset:offset + size])
        n += 1
    return n

base = r"C:\Users\qq289\AppData\Roaming\Tencent\xwechat\radium\users\5645cefdbdd3b13181e19923ec200ab9\applet\packages\wx74b301953dcdd7db\18"
outroot = r"C:\Users\qq289\Documents\nike_mp\unpacked"
for fn in os.listdir(base):
    if not fn.endswith(".wxapkg"): continue
    raw = open(os.path.join(base, fn), "rb").read()
    try:
        dec = decrypt_v1mmwx(raw)
        n = unpack_wxapkg(dec, os.path.join(outroot, fn[:-7]))
        print(f"{fn}: {n} files")
    except AssertionError as e:
        print(f"{fn}: FAILED {e}")
