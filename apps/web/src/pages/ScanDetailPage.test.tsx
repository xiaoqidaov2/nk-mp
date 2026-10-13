import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { nikeCredentialApi, scanApi } from "@/data/api";
import type { NikeCredentialOut, ScanJob, ScanStore } from "@/data/types";

import { ScanDetailPage } from "./ScanDetailPage";

const handlersRef = vi.hoisted(() => ({
  current: null as null | {
    onProgress: (job: ScanJob) => void;
    onDone: () => void;
    onError: (message: string) => void;
    onFallback?: () => void;
  },
}));

vi.mock("@/data/api", () => ({
  scanApi: { get: vi.fn(), stores: vi.fn(), retryFailed: vi.fn() },
  subscribeScanProgress: vi.fn((_id: string, handlers: NonNullable<typeof handlersRef.current>) => {
    handlersRef.current = handlers;
    return () => undefined;
  }),
  nikeCredentialApi: { status: vi.fn() },
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: true, isOps: false, loading: false }),
}));

const jobMock = vi.mocked(scanApi.get);
const storesMock = vi.mocked(scanApi.stores);
const retryMock = vi.mocked(scanApi.retryFailed);
const credentialMock = vi.mocked(nikeCredentialApi.status);

function activeCredential(): NikeCredentialOut {
  return {
    status: "ACTIVE",
    phone_masked: "138****0000",
    access_expires_at: null,
    last_refreshed_at: null,
    last_error_code: null,
    binding_generation: 1,
    login_available: true,
  };
}

function job(overrides: Partial<ScanJob> = {}): ScanJob {
  return {
    id: "job-12345678",
    task_id: "t1",
    task_ids: [],
    kind: "SCAN",
    status: "SUCCEEDED",
    store_codes: [],
    style_codes: [],
    total_stores: 3,
    done_stores: 3,
    failed_stores: 0,
    unknown_stores: 0,
    attempt: 1,
    error_summary: "",
    started_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    created_at: new Date().toISOString(),
    progress: 1,
    ...overrides,
  };
}

function store(overrides: Partial<ScanStore> = {}): ScanStore {
  return {
    store_code: "SH001",
    store_name: "上海南京东路店",
    status: "SUCCEEDED",
    attempt: 1,
    error_code: null,
    message: "",
    observations_written: 3,
    events_written: 0,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/scans/job-12345678"]}>
        <AntdApp>
          <Routes>
            <Route path="/scans/:jobId" element={<ScanDetailPage />} />
          </Routes>
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function clickModalConfirm(name: string) {
  await waitFor(() => expect(document.querySelector(".ant-modal-confirm-btns")).toBeTruthy());
  const buttons = Array.from(
    document.querySelectorAll<HTMLElement>(".ant-modal-confirm-btns .ant-btn"),
  );
  const target = name.replace(/\s+/g, "");
  const match =
    buttons.find((button) => (button.textContent ?? "").replace(/\s+/g, "") === target) ??
    buttons[buttons.length - 1];
  await act(async () => {
    fireEvent.click(match);
  });
}

describe("ScanDetailPage", () => {
  beforeEach(() => {
    handlersRef.current = null;
    jobMock.mockReset();
    storesMock.mockReset();
    retryMock.mockReset();
    credentialMock.mockReset();
    credentialMock.mockResolvedValue(activeCredential());
    storesMock.mockResolvedValue([]);
  });

  it("tells the clerk the progress channel fell back to auto refresh", async () => {
    jobMock.mockResolvedValue(job({ status: "RUNNING", progress: 0.5 }));
    renderPage();

    await screen.findByText(/检查进度 · /);
    act(() => {
      handlersRef.current?.onFallback?.();
    });
    expect(
      await screen.findByText("进度自动更新已中断，正在改为每 3 秒刷新一次。数据仍在更新，无需操作。"),
    ).toBeInTheDocument();

    act(() => {
      handlersRef.current?.onProgress(job({ status: "RUNNING", progress: 0.6 }));
    });
    expect(await screen.findByText("进度自动更新已恢复。")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByText("进度自动更新已中断，正在改为每 3 秒刷新一次。数据仍在更新，无需操作。"),
      ).not.toBeInTheDocument(),
    );
  });

  it("only offers the failed-store retry when there is something to retry", async () => {
    jobMock.mockResolvedValue(job());
    renderPage();

    const button = await screen.findByRole("button", {
      name: /仅重试失败门店|仅 重 试 失 败 门 店/,
    });
    expect(button).toBeDisabled();
  });

  it("retries failed stores after confirming, and reports how many", async () => {
    jobMock.mockResolvedValue(job({ status: "PARTIAL", failed_stores: 2, unknown_stores: 1 }));
    retryMock.mockResolvedValue(job({ status: "PARTIAL", failed_stores: 2, unknown_stores: 1 }));
    renderPage();

    const button = await screen.findByRole("button", {
      name: /仅重试失败门店|仅 重 试 失 败 门 店/,
    });
    expect(button).toBeEnabled();
    await act(async () => {
      fireEvent.click(button);
    });

    expect(
      await screen.findByText("重试 3 家未完成门店？", { selector: ".ant-modal-confirm-title" }),
    ).toBeInTheDocument();
    expect(retryMock).not.toHaveBeenCalled();
    await clickModalConfirm("仅重试失败门店");
    await waitFor(() => expect(retryMock).toHaveBeenCalledWith("job-12345678"));
    expect(await screen.findByText("已重新检查 3 家门店。")).toBeInTheDocument();
  });

  it("translates the store failure reason and keeps the raw code out of the text", async () => {
    jobMock.mockResolvedValue(job({ status: "PARTIAL", failed_stores: 1, done_stores: 2, progress: 1 }));
    storesMock.mockResolvedValue([
      store({ status: "FAILED", error_code: "EMPTY_COMPLETED" }),
    ]);
    renderPage();

    expect(
      await screen.findByText("Nike 返回了空结果，这次不算查到，已按“未探测”处理。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("EMPTY_COMPLETED")).not.toBeInTheDocument();
  });
});
