import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PUSH } from "@/copy";
import { notificationApi } from "@/data/api";
import type { NotificationChannel, NotificationDelivery } from "@/data/types";

import { PushChannelsCard } from "./PushChannelsCard";

vi.mock("@/data/api", () => ({
  notificationApi: {
    channels: vi.fn(),
    createChannel: vi.fn(),
    updateChannel: vi.fn(),
    removeChannel: vi.fn(),
    testChannel: vi.fn(),
    deliveries: vi.fn(),
    retryDelivery: vi.fn(),
  },
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: true, isOps: false, loading: false }),
}));

const channelsMock = vi.mocked(notificationApi.channels);
const createMock = vi.mocked(notificationApi.createChannel);
const updateMock = vi.mocked(notificationApi.updateChannel);
const removeMock = vi.mocked(notificationApi.removeChannel);
const testMock = vi.mocked(notificationApi.testChannel);
const deliveriesMock = vi.mocked(notificationApi.deliveries);
const retryMock = vi.mocked(notificationApi.retryDelivery);

const FAIL_TEXT = "发送失败：平台没有接受这条消息（可在渠道里点“发送测试”复查）。";

function channel(overrides: Partial<NotificationChannel> = {}): NotificationChannel {
  return {
    id: "ch-1",
    platform: "custom",
    name: "本机冒烟",
    target_hint: "••••push",
    enabled: true,
    last_ok_at: null,
    last_error_code: null,
    last_error: null,
    last_error_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function delivery(overrides: Partial<NotificationDelivery> = {}): NotificationDelivery {
  return {
    id: 1,
    job_id: "job-1",
    task_name: "QA 冒烟任务",
    channel_id: "ch-1",
    channel_name: "本机冒烟",
    platform: "custom",
    state: "SENT",
    attempts: 0,
    last_error: null,
    created_at: new Date().toISOString(),
    sent_at: new Date().toISOString(),
    title: "【库存提醒】QA 冒烟任务",
    summary: "补货 2 条",
    ...overrides,
  };
}

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
}

function renderCard(client = makeClient()) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AntdApp>
          <PushChannelsCard />
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

// antd inserts a space between two CJK characters ("删 除"), so match on the
// whitespace-normalised text instead of the accessible name.
function buttonByText(name: string): HTMLElement | null {
  const target = name.replace(/\s+/g, "");
  return (
    screen
      .queryAllByRole("button")
      .find((button) => (button.textContent ?? "").replace(/\s+/g, "") === target) ?? null
  );
}

async function click(text: string): Promise<void> {
  const button = buttonByText(text);
  if (!button) throw new Error(`button "${text}" not found`);
  await act(async () => {
    fireEvent.click(button);
  });
}

async function clickModalConfirm(name: string): Promise<void> {
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

describe("PushChannelsCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    channelsMock.mockResolvedValue([]);
    deliveriesMock.mockResolvedValue([]);
    createMock.mockResolvedValue(channel());
    updateMock.mockResolvedValue(channel());
    removeMock.mockResolvedValue(undefined);
    testMock.mockResolvedValue({ ok: true, error: null });
    retryMock.mockResolvedValue({ ok: true });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("explains how to add the first channel when the user has none", async () => {
    renderCard();
    expect(await screen.findByText(PUSH.empty)).toBeTruthy();
    expect(buttonByText(PUSH.addChannel)).not.toBeNull();
  });

  it("lists a channel with its platform, hint and last result", async () => {
    channelsMock.mockResolvedValue([
      channel({ last_ok_at: "2026-09-30T05:39:08.408265+08:00" }),
    ]);
    deliveriesMock.mockResolvedValue([delivery()]);
    renderCard();
    expect(await screen.findByText(PUSH.platforms.custom.label)).toBeTruthy();
    expect(screen.getByText("••••push")).toBeTruthy();
    expect(screen.getByText(/最近发送/)).toBeTruthy();
    expect(screen.getByText(/QA 冒烟任务/)).toBeTruthy();
    expect(screen.getByText(PUSH.stateSent)).toBeTruthy();
  });

  it("shows the failure text a channel recorded", async () => {
    channelsMock.mockResolvedValue([channel({ last_ok_at: null, last_error: FAIL_TEXT })]);
    renderCard();
    expect(await screen.findByText(FAIL_TEXT)).toBeTruthy();
  });

  it("turns a channel off through the switch", async () => {
    channelsMock.mockResolvedValue([channel()]);
    renderCard();
    const toggle = await screen.findByRole("switch");
    await act(async () => {
      fireEvent.click(toggle);
    });
    expect(updateMock).toHaveBeenCalledWith("ch-1", { enabled: false });
  });

  it("renders only the chosen platform fields and saves the channel", async () => {
    renderCard();
    await screen.findByText(PUSH.empty);
    await click(PUSH.addChannel);
    expect(await screen.findByText(PUSH.fields.access_token)).toBeTruthy();
    expect(screen.getByText(PUSH.fields.secret)).toBeTruthy();
    expect(screen.queryByText(PUSH.fields.smtp_host)).toBeNull();

    const token = screen.getByLabelText(PUSH.fields.access_token);
    fireEvent.change(token, { target: { value: "abc123" } });
    await click(PUSH.saveChannel);

    await waitFor(() =>
      expect(createMock).toHaveBeenCalledWith({
        platform: "dingtalk",
        name: "",
        fields: { access_token: "abc123" },
      }),
    );
  });

  it("reports a successful test message", async () => {
    channelsMock.mockResolvedValue([channel()]);
    renderCard();
    await screen.findByText(PUSH.platforms.custom.label);
    await click(PUSH.sendTest);
    expect(testMock).toHaveBeenCalledWith("ch-1");
    expect(await screen.findByText(PUSH.testOk)).toBeTruthy();
  });

  it("reports the failure reason of a failed test message", async () => {
    channelsMock.mockResolvedValue([channel()]);
    testMock.mockResolvedValue({ ok: false, error: FAIL_TEXT });
    renderCard();
    await screen.findByText(PUSH.platforms.custom.label);
    await click(PUSH.sendTest);
    expect(await screen.findByText(PUSH.testFailed(FAIL_TEXT))).toBeTruthy();
  });

  it("deletes a channel only after the confirmation", async () => {
    channelsMock.mockResolvedValue([channel()]);
    renderCard();
    await screen.findByText(PUSH.platforms.custom.label);
    await click(PUSH.remove);
    await waitFor(() => expect(document.querySelector(".ant-modal-confirm-title")).toBeTruthy());
    expect(
      (document.querySelector(".ant-modal-confirm-title")?.textContent ?? "").replace(/\s+/g, ""),
    ).toBe(PUSH.removeConfirmTitle.replace(/\s+/g, ""));
    expect(removeMock).not.toHaveBeenCalled();
    await clickModalConfirm(PUSH.removeConfirmOk);
    await waitFor(() => expect(removeMock).toHaveBeenCalledWith("ch-1"));
  });

  it("explains an empty push history", async () => {
    renderCard();
    expect(await screen.findByText(PUSH.recentEmpty)).toBeTruthy();
  });
});
