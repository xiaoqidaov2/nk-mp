const CSRF_HEADER = "X-CSRF-Token";
const CSRF_COOKIE = "nike_mp_csrf";
const MUTATING: Record<string, true> = {
  POST: true,
  PUT: true,
  PATCH: true,
  DELETE: true,
};

let csrfToken = "";
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

/** 会话失效（任意请求返回 401）时的全局回调，由 AuthProvider 注入。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function readCsrfCookie(): string {
  const prefix = `${CSRF_COOKIE}=`;
  for (const part of document.cookie.split(";")) {
    const item = part.trim();
    if (item.startsWith(prefix)) {
      return decodeURIComponent(item.slice(prefix.length));
    }
  }
  return "";
}

/** Pulls the stable error contract out of an untrusted response body. */
function parseApiErrorFields(payload: unknown): {
  errorCode: string | null;
  retryAfterSeconds: number | null;
} {
  let errorCode: string | null = null;
  let retryAfterSeconds: number | null = null;
  if (payload !== null && typeof payload === "object") {
    if ("error_code" in payload) {
      const raw = payload.error_code;
      if (typeof raw === "string" && raw.trim()) errorCode = raw;
    }
    if ("retry_after_seconds" in payload) {
      const raw = payload.retry_after_seconds;
      if (typeof raw === "number" && Number.isFinite(raw)) retryAfterSeconds = raw;
      else if (typeof raw === "string" && raw.trim() && !Number.isNaN(Number(raw))) {
        retryAfterSeconds = Number(raw);
      }
    }
  }
  return { errorCode, retryAfterSeconds };
}

export class ApiError extends Error {
  status: number;
  payload: unknown;
  /** Stable server `error_code`; the raw `detail` stays on `message`. */
  errorCode: string | null;
  retryAfterSeconds: number | null;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    const fields = parseApiErrorFields(payload);
    this.errorCode = fields.errorCode;
    this.retryAfterSeconds = fields.retryAfterSeconds;
  }
}

function messageFrom(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (payload && typeof payload === "object") {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      if (first?.msg) return first.msg;
    }
  }
  return fallback;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (MUTATING[method]) {
    // The login response carries the CSRF token, but a page refresh loses
    // module state; the readable cookie set at login is the durable fallback.
    const token = csrfToken || readCsrfCookie();
    if (token) {
      headers.set(CSRF_HEADER, token);
    }
  }

  const response = await fetch(`/api/v1${path}`, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
  });

  if (response.status === 401 && !path.startsWith("/auth/login")) {
    // 会话在别处退出或过期：让全局状态立刻回到登录页，而不是留着假登录界面。
    unauthorizedHandler?.();
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      messageFrom(payload, `请求失败（HTTP ${response.status}）`),
      payload,
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string) => apiRequest<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiRequest<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    apiRequest<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    apiRequest<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  delete: <T>(path: string) => apiRequest<T>(path, { method: "DELETE" }),
};
