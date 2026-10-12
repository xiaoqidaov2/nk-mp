import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { catalogApi, matrixApi, taskApi } from "@/data/api";
import type { Matrix, MonitorTask, StoreOut } from "@/data/types";

import { EditCadenceModal, EditSizeScopeModal, EditStoreScopeModal } from "./TaskScopeModals";

vi.mock("@/data/api", () => ({
  taskApi: { update: vi.fn() },
  catalogApi: { stores: vi.fn(), styles: vi.fn() },
  matrixApi: { get: vi.fn() },
}));

const updateMock = vi.mocked(taskApi.update);
const storesMock = vi.mocked(catalogApi.stores);
const matrixMock = vi.mocked(matrixApi.get);

function task(overrides: Partial<MonitorTask> = {}): MonitorTask {
  return {
    id: "t1",
    name: "CD7069-003",
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
    store_count: 160,
    style_count: 1,
    cooldown_until: null,
    cooldown_reason: null,
    ...overrides,
  };
}

function matrix(sizes: string[]): Matrix {
  return {
    style_code: "CD7069-003",
    title: "CD7069-003",
    image_url: "",
    sizes,
    stores: [],
    cells: [],
    structure_status: "OK",
    last_checked_at: null,
  };
}

function renderModal(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AntdApp>{node}</AntdApp>
    </QueryClientProvider>,
  );
}

/** antd 在两个汉字之间插空格（"保 存"），所以按正则取按钮。 */
async function clickOk() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  });
}

/**
 * 开关按索引取（jsdom 零宽度会把多选标签折叠，故不断言标签文本）：
 * 选码弹窗只有 0；选门店弹窗 0=只看换季优惠店、1=全部门店。
 */
function scopeSwitch(index = 0): HTMLElement {
  const element = document.querySelectorAll(".ant-switch")[index];
  if (!element) throw new Error(`switch ${index} not found`);
  return element as HTMLElement;
}

describe("TaskScopeModals", () => {
  beforeEach(() => {
    updateMock.mockReset();
    storesMock.mockReset();
    matrixMock.mockReset();
    updateMock.mockResolvedValue(task());
    storesMock.mockResolvedValue([
      { store_code: "3402", display_name: "NIKE北京朝阳园", address: "", concept: "NVS", is_active: true },
    ] as StoreOut[]);
    matrixMock.mockResolvedValue(matrix(["40", "41", "42"]));
  });

  it("选码：默认全部尺码，保存空数组（=全部）", async () => {
    renderModal(<EditSizeScopeModal task={task()} open onClose={() => {}} />);
    await screen.findByText(/选择尺码/);
    await waitFor(() => expect(scopeSwitch()).toHaveAttribute("aria-checked", "true"));
    await clickOk();
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("t1", { size_labels: [] }));
  });

  it("选码：任务已指定尺码时开关关闭并原样回传", async () => {
    renderModal(
      <EditSizeScopeModal task={task({ size_labels: ["41"] })} open onClose={() => {}} />,
    );
    await screen.findByText(/选择尺码/);
    await waitFor(() => expect(scopeSwitch()).toHaveAttribute("aria-checked", "false"));
    await clickOk();
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("t1", { size_labels: ["41"] }));
  });

  it("选门店：全部门店时保存空数组（后端转成 all_stores）", async () => {
    renderModal(<EditStoreScopeModal task={task()} open onClose={() => {}} />);
    await screen.findByText(/选择门店/);
    await waitFor(() => expect(scopeSwitch(1)).toHaveAttribute("aria-checked", "true"));
    await clickOk();
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("t1", { store_codes: [] }));
  });

  it("选门店：任务已指定门店时开关关闭并原样回传", async () => {
    renderModal(
      <EditStoreScopeModal
        task={task({ all_stores: false, store_codes: ["3402"], store_count: 1 })}
        open
        onClose={() => {}}
      />,
    );
    await screen.findByText(/选择门店/);
    await waitFor(() => expect(scopeSwitch(1)).toHaveAttribute("aria-checked", "false"));
    await clickOk();
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("t1", { store_codes: ["3402"] }),
    );
  });

  it("改间隔：把选中的档位写回 cadence_seconds（后端一直支持，之前前端没入口）", async () => {
    renderModal(<EditCadenceModal task={task({ cadence_seconds: 600 })} open onClose={() => {}} />);
    await screen.findByText(/检查间隔/);
    // 默认带出当前值 10 分钟，直接保存应原样回传
    await clickOk();
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("t1", { cadence_seconds: 600 }));
  });

  it("改间隔：给出不低于建议值的提示（160 店 × 1 货号的建议是 2 分钟）", async () => {
    renderModal(
      <EditCadenceModal task={task({ cadence_seconds: 30, store_count: 160 })} open onClose={() => {}} />,
    );
    await screen.findByText(/检查间隔/);
    expect(await screen.findByText(/建议不小于 2 分钟/)).toBeTruthy();
  });

  it("选门店：默认只查换季优惠店，打开开关改为查全部", async () => {
    renderModal(<EditStoreScopeModal task={task()} open onClose={() => {}} />);
    await screen.findByText(/选择门店/);
    await waitFor(() => expect(storesMock).toHaveBeenCalledWith("", "clearance"));
    await act(async () => {
      fireEvent.click(scopeSwitch(0));
    });
    await waitFor(() => expect(storesMock).toHaveBeenCalledWith("", "all"));
  });
});
