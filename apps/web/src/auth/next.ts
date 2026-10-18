/** 登录后要回到的站内路径；只接受站内绝对路径，防止开放重定向。 */
export function safeNext(raw: string | null | undefined, fallback = "/dashboard"): string {
  if (!raw) return fallback;
  // 必须以单个 "/" 开头：//evil.com 与 /\evil.com 都会被浏览器当成外站。
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return fallback;
  if (raw === "/login" || raw.startsWith("/login?")) return fallback;
  return raw;
}
