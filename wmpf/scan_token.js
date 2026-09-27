// 内存扫描 v2: 裸词 refreshToken, 兼容转义 JSON
const WORD = "refreshToken";
const needle = [];
for (let i = 0; i < WORD.length; i++) needle.push(WORD.charCodeAt(i));
const pattern = needle.map(c => ("0" + c.toString(16)).slice(-2)).join(" ");

const ranges = Process.enumerateRanges("rw-");
send("[scan] rw 区间: " + ranges.length);
let found = 0, saved = 0;
for (const r of ranges) {
  if (r.size > 512 * 1024 * 1024) continue;
  let hits = [];
  try { hits = Memory.scanSync(r.base, r.size, pattern); } catch (e) { continue; }
  for (const m of hits) {
    found++;
    if (saved >= 3) continue;
    try {
      const ctxStart = m.address.sub(0x400);
      const buf = ctxStart.readByteArray(0x1000);
      const bytes = new Uint8Array(buf);
      let s = "";
      for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      const m1 = s.match(/refreshToken[\\"':\s]{1,6}([A-Za-z0-9._\-]{16,400})/);
      if (m1 && m1[1] && m1[1] !== "null") {
        send("[REFRESH_TOKEN_FULL]" + m1[1]);
        saved++;
      } else {
        const idx = s.indexOf("refreshToken");
        send("[ctx] " + s.slice(Math.max(0, idx - 80), idx + 200).replace(/[^\x20-\x7e]/g, "."));
      }
    } catch (e) {}
  }
}
send("[scan] 完成: 命中 " + found + " 处, 保存 " + saved);
