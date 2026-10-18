import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { matrixApi, scanApi, taskApi } from "@/data/api";
import type { Matrix, MatrixCell, MonitorTask, ScanJob } from "@/data/types";

import { TaskDetailPage } from "./TaskDetailPage";

vi.mock("@/data/api", () => ({
  taskApi: { list: vi.fn(), update: vi.fn(), remove: vi.fn() },
  scanApi: { list: vi.fn(), create: vi.fn() },
  matrixApi: { get: vi.fn() },
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: true, isOps: false, loading: false }),
}));

vi.mock("@/components/NikeCredentialCard", () => ({
  NikeConnectionNotice: () => null,
  isCredentialActive: (status?: string) => status === "ACTIVE",
  useNikeCredentialsQuery: () => ({ data: [{ status: "ACTIVE" }] }),
}));

vi.mock("@/components/PushChannelsCard", () => ({
  useHasEnabledChannel: () => true,
}));

const tasksMock = vi.mocked(taskApi.list);
const scansMock = vi.mocked(scanApi.list);
const matrixMock = vi.mocked(matrixApi.get);

function task(): MonitorTask {
  return {
    id: "t1",
    name: "监控任务",
    store_codes: [],
    style_codes: ["CD7069-003"],
    size_labels: [],
    all_stores: true,
    cadence_seconds: 30,
    is_active: true,
    notify_on_restock: true,
    notify_on_sold_out: true,
    notify_on_price_change: true,
    next_run_at: null,
    last_run_at: null,
    created_at: "2026-10-06T12:00:00+00:00",
    store_count: 2,
    style_count: 1,
    cooldown_until: null,
    cooldown_reason: null,
  };
}

function cell(storeCode: string, size: string, availability: MatrixCell["availability"]): MatrixCell {
  return {
    store_code: storeCode,
    store_name: "",
    size_label: size,
    availability,
    trust: "TRUSTED",
    price_status: "MISSING",
    price: null,
    total_price: null,
    promotions: [],
    error_code: null,
    checked_at: "2026-10-06T12:00:00+00:00",
  };
}

function matrix(cells: MatrixCell[], stores = ["S-A", "S-B"]): Matrix {
  return {
    style_code: "CD7069-003",
    title: "CD7069-003",
    image_url: "",
    sizes: ["40", "41"],
    stores: stores.map((code) => ({ store_code: code, store_name: "" })),
    cells,
    structure_status: "OK",
    last_checked_at: "2026-10-06T12:00:00+00:00",
  };
}

function scan(): ScanJob {
  return {
    id: "6f0b1c22-0000-0000-0000-000000000001",
    status: "SUCCEEDED",
    kind: "MONITOR",
    task_id: "t1",
    task_ids: [],
    store_codes: ["S1", "S2"],
    style_codes: ["DN3707-202"],
    total_stores: 2,
    done_stores: 2,
    failed_stores: 0,
    unknown_stores: 0,
    attempt: 0,
    error_summary: "",
    started_at: "2026-10-06T11:59:00+00:00",
    finished_at: "2026-10-06T12:00:00+00:00",
    created_at: "2026-10-06T12:00:00+00:00",
    progress: 1,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/tasks/t1"]}>
        <AntdApp>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          </Routes>
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TaskDetailPage", () => {
  beforeEach(() => {
    tasksMock.mockReset();
    scansMock.mockReset();
    matrixMock.mockReset();
    tasksMock.mockResolvedValue([task()]);
    scansMock.mockResolvedValue([scan()]);
  });

  it("各门店货源排在检查记录之前，且按在货码数降序", async () => {
    matrixMock.mockResolvedValue(
      matrix([
        cell("S-A", "40", "IN_STOCK"),
        cell("S-B", "40", "IN_STOCK"),
        cell("S-B", "41", "IN_STOCK"),
      ]),
    );
    const { container } = renderPage();
    await screen.findByText("各门店货源");
    const text = container.textContent ?? "";
    expect(text.indexOf("各门店货源")).toBeLessThan(text.indexOf("这个任务的检查记录"));
    expect(text.indexOf("S-B")).toBeLessThan(text.indexOf("S-A"));
  });

  it("没有在货门店时仍逐店列出并标注无货", async () => {
    matrixMock.mockResolvedValue(matrix([cell("S-A", "40", "NOT_CARRIED")]));
    renderPage();
    expect((await screen.findAllByText("无货")).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("还没有各门店货源。运行一次检查后再看。")).not.toBeInTheDocument();
  });

  it("完全没有门店数据时给出空态", async () => {
    matrixMock.mockResolvedValue(matrix([], []));
    renderPage();
    expect(
      await screen.findByText("还没有各门店货源。运行一次检查后再看。"),
    ).toBeInTheDocument();
  });
});
