import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { catalogApi, matrixApi, nikeCredentialApi, scanApi, taskApi } from "@/data/api";
import type { MonitorTask, NikeCredentialOut, ScanJob } from "@/data/types";

import { TasksPage } from "./TasksPage";

vi.mock("@/data/api", () => ({
  taskApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  scanApi: { list: vi.fn(), create: vi.fn() },
  nikeCredentialApi: { list: vi.fn() },
  catalogApi: { stores: vi.fn(), styles: vi.fn() },
  matrixApi: { get: vi.fn() },
}));

const state = vi.hoisted(() => ({ canWrite: true }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: state.canWrite, isOps: false, loading: false }),
}));

const tasksMock = vi.mocked(taskApi.list);
const scansMock = vi.mocked(scanApi.list);
const removeMock = vi.mocked(taskApi.remove);
const credentialMock = vi.mocked(nikeCredentialApi.list);
const storesMock = vi.mocked(catalogApi.stores);
const matrixMock = vi.mocked(matrixApi.get);

const EMPTY_TASK_LIST = "还没有监控任务。创建后按周期自动检查指定货号与门店。";
const READONLY = "你当前是只读成员，可以查看结果但不能新建或修改任务。";

function activeCredential(): NikeCredentialOut {
  return {
    id: "cred-1",
    status: "ACTIVE",
    phone_masked: "138****0000",
    access_expires_at: null,
    last_refreshed_at: null,
    last_error_code: null,
    binding_generation: 1,
    cooldown_until: null,
    cooldown_reason: null,
    login_available: true,
  };
}

function task(overrides: Partial<MonitorTask> = {}): MonitorTask {
  return {
    id: "t1",
    name: "测试任务",
    store_codes: [],
    style_codes: ["DN3707-202"],
    size_labels: [],
    all_stores: true,
    cadence_seconds: 1800,
    is_active: true,
    notify_on_restock: true,
    notify_on_sold_out: true,
    notify_on_price_change: true,
    next_run_at: null,
    last_run_at: null,
    created_at: new Date().toISOString(),
    store_count: 5,
    style_count: 1,
    cooldown_until: null,
    cooldown_reason: null,
    ...overrides,
  };
}

function scanJob(overrides: Partial<ScanJob> = {}): ScanJob {
  return {
    id: "job-1",
    task_id: null,
    task_ids: [],
    kind: "MONITOR",
    status: "SUCCEEDED",
    store_codes: [],
    style_codes: [],
    total_stores: 0,
    done_stores: 0,
    failed_stores: 0,
    unknown_stores: 0,
    attempt: 0,
    error_summary: "",
    started_at: null,
    finished_at: null,
    created_at: new Date().toISOString(),
    progress: 0,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AntdApp>
          <TasksPage />
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function clickButton(name: string) {
  const buttons = screen.queryAllByRole("button");
  const matches = buttons.filter(
    (button) => (button.textContent ?? "").replace(/\s+/g, "") === name,
  );
  if (matches.length === 0) {
    throw new Error(`button "${name}" not found`);
  }
  await act(async () => {
    fireEvent.click(matches[matches.length - 1]);
  });
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

describe("TasksPage", () => {
  beforeEach(() => {
    state.canWrite = true;
    tasksMock.mockReset();
    scansMock.mockReset();
    removeMock.mockReset();
    credentialMock.mockReset();
    tasksMock.mockResolvedValue([]);
    scansMock.mockResolvedValue([]);
    credentialMock.mockResolvedValue([activeCredential()]);
    storesMock.mockResolvedValue([]);
    matrixMock.mockResolvedValue({
      style_code: "DN3707-202",
      title: "DN3707-202",
      image_url: "",
      sizes: ["40", "41"],
      stores: [],
      cells: [],
      structure_status: "OK",
      last_checked_at: null,
    });
  });

  it("操作列提供 选码 / 选门店 / 暂停，点选码打开弹窗", async () => {
    tasksMock.mockResolvedValue([task()]);
    renderPage();
    const sizeButton = await screen.findByRole("button", { name: "选码" });
    expect(screen.getByRole("button", { name: "选门店" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(sizeButton);
    });
    expect(await screen.findByText(/选择尺码/)).toBeInTheDocument();
  });

  it("shows the empty state sentence while the task list is empty", async () => {
    renderPage();
    expect(await screen.findByText(EMPTY_TASK_LIST)).toBeInTheDocument();
    expect(
      await screen.findByText("还没有检查记录。创建任务并立即检查后，这里会显示结果。"),
    ).toBeInTheDocument();
  });

  it("tells read-only members what they can and cannot do", async () => {
    state.canWrite = false;
    renderPage();
    const readonly = await screen.findAllByText(READONLY);
    expect(readonly.length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /新建监控任务|新 建 监 控 任 务/ })).toBeNull();
  });

  it("explains why immediate check is disabled instead of a silent dead button", async () => {
    tasksMock.mockResolvedValue([task({ style_count: 0, style_codes: [] })]);
    renderPage();

    const button = await screen.findByRole("button", { name: /立即检查|立 即 检 查/ });
    expect(button).toBeDisabled();
    fireEvent.mouseOver(button.parentElement as HTMLElement);
    expect(await screen.findByText("任务还没有货号，先编辑任务")).toBeInTheDocument();
  });

  it("deletes a task only after the confirmation dialog", async () => {
    tasksMock.mockResolvedValue([task()]);
    removeMock.mockResolvedValue(undefined);
    renderPage();

    await screen.findByText("测试任务");
    await clickButton("删除任务");
    expect(
      await screen.findByText("删除这个监控任务？", { selector: ".ant-modal-confirm-title" }),
    ).toBeInTheDocument();
    expect(removeMock).not.toHaveBeenCalled();

    await clickModalConfirm("删除任务");
    await waitFor(() => expect(removeMock).toHaveBeenCalledWith("t1"));
    expect(await screen.findByText("已删除「测试任务」。")).toBeInTheDocument();
  });

  it("shows a cooling badge with the auto-resume moment when the account is throttled", async () => {
    const until = new Date(Date.now() + 30 * 60_000).toISOString();
    tasksMock.mockResolvedValue([
      task({ cooldown_until: until, cooldown_reason: "throttled", cadence_seconds: 30 }),
    ]);
    renderPage();

    const badge = await screen.findByText(/冷却中 · 将在 \d{2}:\d{2} 自动继续/);
    expect(badge).toBeInTheDocument();
    expect(await screen.findByText("30 秒")).toBeInTheDocument();
  });

  it("labels a merged job by how many tasks it covers, not as a manual check", async () => {
    tasksMock.mockResolvedValue([
      task({ id: "t1", name: "任务 A" }),
      task({ id: "t2", name: "任务 B" }),
    ]);
    scansMock.mockResolvedValue([
      scanJob({ id: "job-merged", task_ids: ["t1", "t2"] }),
      scanJob({ id: "job-manual", task_ids: [] }),
      scanJob({ id: "job-single", task_id: "t1", task_ids: [] }),
    ]);
    renderPage();

    expect(await screen.findByText("合并检查 2 个任务")).toBeInTheDocument();
    expect(await screen.findByText("手动检查")).toBeInTheDocument();
    // 单任务作业仍显示任务名（任务表里也有同名行，所以用 findAll）
    expect((await screen.findAllByText("任务 A")).length).toBeGreaterThan(1);
  });
});
