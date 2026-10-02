import type { Mock } from "vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, setCsrfToken, setUnauthorizedHandler } from "./client";

function jsonResponse(status: number, body: string): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => body,
  } as unknown as Response;
}

function headerOf(fetchMock: Mock, name: string): string | null {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return new Headers(init.headers).get(name);
}

describe("apiRequest CSRF handling", () => {
  beforeEach(() => {
    document.cookie = "nike_mp_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
    setCsrfToken("");
  });

  afterEach(() => {
    setUnauthorizedHandler(null);
    vi.unstubAllGlobals();
  });

  it("uses the readable cookie after a page refresh", async () => {
    document.cookie = "nike_mp_csrf=cookie-token; path=/";
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, "{}"));
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/tasks", { method: "POST", body: "{}" });

    expect(headerOf(fetchMock, "X-CSRF-Token")).toBe("cookie-token");
  });

  it("prefers the in-memory token from the login response", async () => {
    document.cookie = "nike_mp_csrf=stale-cookie; path=/";
    setCsrfToken("fresh-token");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, "{}"));
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/tasks", { method: "PATCH", body: "{}" });

    expect(headerOf(fetchMock, "X-CSRF-Token")).toBe("fresh-token");
  });

  it("does not attach the CSRF header to reads", async () => {
    document.cookie = "nike_mp_csrf=cookie-token; path=/";
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, "[]"));
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/tasks");

    expect(headerOf(fetchMock, "X-CSRF-Token")).toBeNull();
  });

  it("notifies the app when the session is gone", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(401, '{"detail":"not authenticated"}')),
    );

    await expect(apiRequest("/tasks")).rejects.toThrow();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("keeps a failed login local to the login page", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(401, '{"detail":"邮箱或密码不正确"}')),
    );

    await expect(apiRequest("/auth/login", { method: "POST", body: "{}" })).rejects.toThrow();

    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
