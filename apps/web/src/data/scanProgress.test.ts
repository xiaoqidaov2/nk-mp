// Subscribe-to-progress behaviour: SSE frames plus polling fallback.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { ScanJob } from "./types";

import { subscribeScanProgress } from "./api";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  listeners: Record<string, ((event: MessageEvent) => void)[]> = {};
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(listener);
  }

  emit(type: string, data: string) {
    for (const listener of this.listeners[type] ?? []) {
      listener({ data } as MessageEvent);
    }
  }

  close() {
    this.closed = true;
  }
}

const job = (status: ScanJob["status"]): ScanJob =>
  ({
    id: "job-1",
    task_id: "task-1",
    task_ids: [],
    kind: "SCAN",
    status,
    store_codes: [],
    style_codes: [],
    total_stores: 0,
    done_stores: 0,
    failed_stores: 0,
    unknown_stores: 0,
    attempt: 1,
    error_summary: "",
    started_at: null,
    finished_at: null,
    created_at: new Date().toISOString(),
    progress: 0.5,
  }) as ScanJob;

describe("subscribeScanProgress", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    FakeEventSource.instances = [];
  });

  it("delivers SSE progress frames and finishes on done", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onProgress = vi.fn();
    const onDone = vi.fn();
    const unsubscribe = subscribeScanProgress("job-1", {
      onProgress,
      onDone,
      onError: vi.fn(),
    });

    const source = FakeEventSource.instances[0];
    expect(source.url).toBe("/api/v1/scans/job-1/events");
    source.emit("progress", JSON.stringify(job("RUNNING")));
    expect(onProgress).toHaveBeenCalledTimes(1);

    source.emit("done", "{}");
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(source.closed).toBe(true);
    unsubscribe();
  });

  it("falls back to polling when the stream errors", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.useFakeTimers();
    const onProgress = vi.fn();
    const onDone = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        text: async () => JSON.stringify(job("SUCCEEDED")),
      }),
    );

    const unsubscribe = subscribeScanProgress("job-1", {
      onProgress,
      onDone,
      onError: vi.fn(),
    });
    FakeEventSource.instances[0].onerror?.();
    await vi.advanceTimersByTimeAsync(3000);

    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});
