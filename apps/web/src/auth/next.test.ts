import { describe, expect, it } from "vitest";

import { safeNext } from "./next";

describe("safeNext", () => {
  it("keeps an in-app path so a pushed task link survives login", () => {
    expect(safeNext("/tasks/1de593d5-b698-4be3-b4e2-ce5ed3031e65")).toBe(
      "/tasks/1de593d5-b698-4be3-b4e2-ce5ed3031e65",
    );
    expect(safeNext("/tasks?tab=active")).toBe("/tasks?tab=active");
  });

  it("falls back to the dashboard for missing or external targets", () => {
    for (const raw of [null, undefined, "", "https://evil.example/x", "//evil.example", "/\\evil.example"]) {
      expect(safeNext(raw)).toBe("/dashboard");
    }
  });

  it("never loops back to the login page", () => {
    expect(safeNext("/login")).toBe("/dashboard");
    expect(safeNext("/login?next=/tasks")).toBe("/dashboard");
  });
});
