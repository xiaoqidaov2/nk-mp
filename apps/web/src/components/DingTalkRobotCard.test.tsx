import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PUSH } from "@/copy";
import { notificationApi } from "@/data/api";

import { DingTalkRobotCard } from "./DingTalkRobotCard";

vi.mock("@/data/api", () => ({
  notificationApi: {
    dingtalkRobot: vi.fn(),
    saveDingTalkRobot: vi.fn(),
    removeDingTalkRobot: vi.fn(),
    dingtalkLinks: vi.fn(),
    removeDingTalkLink: vi.fn(),
  },
}));

const robotMock = vi.mocked(notificationApi.dingtalkRobot);
const saveRobotMock = vi.mocked(notificationApi.saveDingTalkRobot);
const removeRobotMock = vi.mocked(notificationApi.removeDingTalkRobot);
const linksMock = vi.mocked(notificationApi.dingtalkLinks);
const removeLinkMock = vi.mocked(notificationApi.removeDingTalkLink);

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
}

function renderCard(client = makeClient()) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AntdApp>
          <DingTalkRobotCard />
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

// antd inserts a space between two CJK characters ("保 存"), so match on the
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

describe("DingTalkRobotCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    robotMock.mockResolvedValue({ configured: false, robot_code: "" });
    saveRobotMock.mockResolvedValue({ configured: true, robot_code: "ding_app_key" });
    removeRobotMock.mockResolvedValue(undefined);
    linksMock.mockResolvedValue([]);
    removeLinkMock.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("marks the enterprise robot card as optional", async () => {
    renderCard();
    expect(await screen.findByText(PUSH.dingtalkTitle)).toBeTruthy();
    expect(screen.getByText(PUSH.dingtalkOptionalTag)).toBeTruthy();
  });

  it("saves the dingtalk robot credentials the customer fills in", async () => {
    renderCard();
    await screen.findByText(PUSH.dingtalkNote);
    fireEvent.change(screen.getByPlaceholderText(PUSH.dingtalkKey), {
      target: { value: "ding_app_key" },
    });
    fireEvent.change(screen.getByPlaceholderText(PUSH.dingtalkSecret), {
      target: { value: "secret-value" },
    });
    await click(PUSH.dingtalkSave);
    await waitFor(() =>
      expect(saveRobotMock).toHaveBeenCalledWith({
        robot_code: "ding_app_key",
        robot_secret: "secret-value",
      }),
    );
    expect(await screen.findByText(PUSH.dingtalkSaved("ding_app_key"))).toBeTruthy();
  });

  it("lists bound conversations and unbinds only after confirmation", async () => {
    robotMock.mockResolvedValue({ configured: true, robot_code: "ding_app_key" });
    linksMock.mockResolvedValue([
      {
        id: "l-1",
        conversation_title: "测试群",
        conversation_type: 2,
        created_at: new Date().toISOString(),
      },
    ]);
    renderCard();
    expect(await screen.findByText("测试群")).toBeTruthy();
    expect(screen.getByText(PUSH.dingtalkSaved("ding_app_key"))).toBeTruthy();
    await click(PUSH.dingtalkUnbind);
    expect(removeLinkMock).not.toHaveBeenCalled();
    await clickModalConfirm(PUSH.dingtalkUnbindConfirmOk);
    await waitFor(() => expect(removeLinkMock).toHaveBeenCalledWith("l-1"));
  });

  it("deletes the robot credentials only after confirmation", async () => {
    robotMock.mockResolvedValue({ configured: true, robot_code: "ding_app_key" });
    renderCard();
    await screen.findByText(PUSH.dingtalkSaved("ding_app_key"));
    await click(PUSH.dingtalkRemove);
    expect(removeRobotMock).not.toHaveBeenCalled();
    await clickModalConfirm(PUSH.dingtalkRemoveConfirmOk);
    await waitFor(() => expect(removeRobotMock).toHaveBeenCalled());
  });
});
