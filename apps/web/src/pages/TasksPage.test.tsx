import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { nikeCredentialApi, scanApi, taskApi } from "@/data/api";
import type { MonitorTask, NikeCredentialOut } from "@/data/types";

import { TasksPage } from "./TasksPage";

vi.mock("@/data/api", () => ({
  taskApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  scanApi: { list: vi.fn(), create: vi.fn() },
  nikeCredentialApi: { status: vi.fn() },
}));

const state = vi.hoisted(() => ({ canWrite: true }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: state.canWrite, isOps: false, loading: false }),
}));

const tasksMock = vi.mocked(taskApi.list);
const scansMock = vi.mocked(scanApi.list);
const removeMock = vi.mocked(taskApi.remove);
const credentialMock = vi.mocked(nikeCredentialApi.status);

const EMPTY_TASK_LIST = "还没有监控任务。创建后按周期自动检查指定货号与门店。";
const READONLY = "你当前是只读成员，可以查看结果但不能新建或修改任务。";

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

function task(overrides: Partial<MonitorTask> = {}): MonitorTask {
  return {
    id: "t1",
    name: "测试任务",
    store_codes: [],
    style_codes: ["DN3707-202"],
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
    credentialMock.mockResolvedValue(activeCredential());
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
});
