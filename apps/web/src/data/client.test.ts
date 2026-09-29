import type { Mock } from "vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, setCsrfToken } from "./client";

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
});
