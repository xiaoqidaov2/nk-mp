import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { catalogApi, nikeCredentialApi, scanApi, taskApi } from "@/data/api";
import type { MonitorTask, NikeCredentialOut, ScanJob } from "@/data/types";

import { TaskCreatePage } from "./TaskCreatePage";

vi.mock("@/data/api", () => ({
  catalogApi: { stores: vi.fn(), styles: vi.fn() },
  taskApi: { create: vi.fn() },
  scanApi: { create: vi.fn() },
  nikeCredentialApi: { status: vi.fn() },
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: true, isOps: false, loading: false }),
}));

const storesMock = vi.mocked(catalogApi.stores);
const stylesMock = vi.mocked(catalogApi.styles);
const createTaskMock = vi.mocked(taskApi.create);
const createScanMock = vi.mocked(scanApi.create);
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

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AntdApp>
          <TaskCreatePage />
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function clickButton(name: string) {
  // antd inserts a literal space between two CJK characters ("添 加"), so
  // match on whitespace-normalised text instead of the accessible name.
  const buttons = screen.queryAllByRole("button");
  const matches = buttons.filter(
    (button) => (button.textContent ?? "").replace(/\s+/g, "") === name,
  );
  if (matches.length === 0) {
    const available = buttons.map((button) => JSON.stringify(button.textContent)).join(", ");
    throw new Error(`button "${name}" not found; available: ${available}`);
  }
  // Step transitions await async validation, so let React flush before the
  // next assertion or click.
  await act(async () => {
    fireEvent.click(matches[matches.length - 1]);
  });
}

describe("TaskCreatePage wizard", () => {
  beforeEach(() => {
    storesMock.mockResolvedValue([]);
    stylesMock.mockResolvedValue([]);
    createTaskMock.mockResolvedValue({ id: "task-1" } as MonitorTask);
    createScanMock.mockResolvedValue({ id: "job-1" } as ScanJob);
    credentialMock.mockResolvedValue(activeCredential());
  });

  it("submits every field that was entered on the earlier steps", async () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/粘贴货号/), {
      target: { value: "DN3707-202" },
    });
    await clickButton("添加");
    // The name lives on step 1 together with the codes and stores.
    const nameInput = document.getElementById("name") as HTMLInputElement;
    expect(nameInput).toBeTruthy();
    fireEvent.change(nameInput, { target: { value: "我的监控任务" } });
    await clickButton("下一步");
    // Deliberately skip store selection: empty means "all synced stores".
    expect(document.getElementById("name")).toBeTruthy();
    await clickButton("创建任务");

    await waitFor(() => expect(createTaskMock).toHaveBeenCalledTimes(1));
    // Regression: the confirm step used to submit an empty object because the
    // form had been unmounted, losing the mandatory name field.
    expect(createTaskMock).toHaveBeenCalledWith({
      name: "我的监控任务",
      cadence_seconds: 1800,
      notify_on_restock: true,
      notify_on_sold_out: true,
      notify_on_price_change: true,
      style_codes: ["DN3707-202"],
      store_codes: [],
    });
    await waitFor(() => expect(createScanMock).toHaveBeenCalledWith({ task_id: "task-1" }));
  });

  it("keeps the entered name when navigating back and forth", async () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/粘贴货号/), {
      target: { value: "IO5031-600" },
    });
    await clickButton("添加");
    const nameInput = document.getElementById("name") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "往返任务" } });
    await clickButton("下一步");
    await clickButton("上一步");
    await clickButton("下一步");

    expect((document.getElementById("name") as HTMLInputElement).value).toBe("往返任务");
  });

  it("blocks the first step until at least one style code is added", async () => {
    renderPage();

    const next = await screen.findByRole("button", { name: /下一步|下 一 步/ });
    expect(next).toBeDisabled();
    expect(screen.getByText("请先添加至少 1 个货号。")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/粘贴货号/), {
      target: { value: "DN3707-202, IO5031-600" },
    });
    await clickButton("添加");
    expect(next).toBeEnabled();
    expect(screen.getByText("共 2 个货号。")).toBeInTheDocument();
  });
});
