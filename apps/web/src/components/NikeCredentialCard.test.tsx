import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/data/client";
import { nikeCredentialApi } from "@/data/api";
import type { NikeAuthAttemptOut, NikeBootstrapOut, NikeCredentialOut } from "@/data/types";

import { NikeCredentialCard } from "./NikeCredentialCard";

vi.mock("@/data/api", () => ({
  nikeCredentialApi: {
    list: vi.fn(),
    start: vi.fn(),
    attempt: vi.fn(),
    verify: vi.fn(),
    cancel: vi.fn(),
    disconnect: vi.fn(),
    bootstrap: vi.fn(),
    startBootstrap: vi.fn(),
  },
}));

const state = vi.hoisted(() => ({
  user: { id: "user-a" } as { id: string },
  canWrite: true,
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: state.user, canWrite: state.canWrite, isOps: false, loading: false }),
}));

const listMock = vi.mocked(nikeCredentialApi.list);
const startMock = vi.mocked(nikeCredentialApi.start);
const attemptMock = vi.mocked(nikeCredentialApi.attempt);
const verifyMock = vi.mocked(nikeCredentialApi.verify);
const cancelMock = vi.mocked(nikeCredentialApi.cancel);
const disconnectMock = vi.mocked(nikeCredentialApi.disconnect);
const bootstrapMock = vi.mocked(nikeCredentialApi.bootstrap);
const startBootstrapMock = vi.mocked(nikeCredentialApi.startBootstrap);

const CONSENT = "我同意本平台使用我的 Nike 账号进行只读采集并保存加密续期凭证";

function credential(overrides: Partial<NikeCredentialOut> = {}): NikeCredentialOut {
  return {
    id: "cred-1",
    status: "NOT_CONNECTED",
    phone_masked: null,
    access_expires_at: null,
    last_refreshed_at: null,
    last_error_code: null,
    binding_generation: null,
    cooldown_until: null,
    cooldown_reason: null,
    login_available: true,
    ...overrides,
  };
}

function plan(overrides: Partial<NikeBootstrapOut> = {}): NikeBootstrapOut {
  return {
    state: "NONE",
    progress: 0,
    stage: "",
    store_count: 0,
    error_code: null,
    updated_at: null,
    ...overrides,
  };
}

function attempt(overrides: Partial<NikeAuthAttemptOut> = {}): NikeAuthAttemptOut {
  return {
    id: "attempt-1",
    state: "CODE_SENT",
    phone_masked: "138****0000",
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    resend_after: new Date(Date.now() + 600_000).toISOString(),
    attempts_remaining: 5,
    error_code: null,
    ...overrides,
  };
}

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
}

function renderCard(client = makeClient()) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AntdApp>
          <NikeCredentialCard />
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

// antd inserts a space between two CJK characters ("取 消"), so match on the
// whitespace-normalised text instead of the accessible name.
function buttonByText(name: string): HTMLElement | null {
  const target = name.replace(/\s+/g, "");
  return (
    screen
      .queryAllByRole("button")
      .find((button) => (button.textContent ?? "").replace(/\s+/g, "") === target) ?? null
  );
}

function buttonMatching(pattern: RegExp): HTMLElement | null {
  return (
    screen
      .queryAllByRole("button")
      .find((button) => pattern.test((button.textContent ?? "").replace(/\s+/g, ""))) ?? null
  );
}

async function click(text: string): Promise<void> {
  const button = buttonByText(text);
  if (!button) throw new Error(`button "${text}" not found`);
  await act(async () => {
    fireEvent.click(button);
  });
}

/** 绑定流程在弹窗内进行：先点卡片入口（等凭据查询落定），再操作弹窗。 */
async function openConnectModal(entry = "连接 Nike 账号"): Promise<void> {
  await waitFor(() => expect(buttonByText(entry)).not.toBeNull());
  await click(entry);
  await waitFor(() => expect(document.querySelector(".ant-modal-content")).toBeTruthy());
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

async function fillPhoneAndConsent(phone = "13800000000"): Promise<void> {
  fireEvent.change(await screen.findByPlaceholderText("+8613800000000"), {
    target: { value: phone },
  });
  act(() => {
    fireEvent.click(screen.getByRole("checkbox"));
  });
}

describe("NikeCredentialCard", () => {
  beforeEach(() => {
    state.user = { id: "user-a" };
    state.canWrite = true;
    sessionStorage.clear();
    listMock.mockReset();
    startMock.mockReset();
    attemptMock.mockReset();
    verifyMock.mockReset();
    cancelMock.mockReset();
    disconnectMock.mockReset();
    bootstrapMock.mockReset();
    startBootstrapMock.mockReset();
    listMock.mockResolvedValue([]);
    attemptMock.mockResolvedValue(attempt());
    bootstrapMock.mockResolvedValue(plan());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("walks from not connected through a wrong code to a successful connection", async () => {
    startMock.mockResolvedValue(attempt());
    bootstrapMock.mockResolvedValue(
      plan({ state: "SUCCEEDED", progress: 1, stage: "门店数据已缓存（160 家）", store_count: 160 }),
    );
    verifyMock
      .mockRejectedValueOnce(
        new ApiError(400, "验证码不正确", {
          detail: "验证码不正确",
          error_code: "NIKE_CODE_INVALID",
          retry_after_seconds: null,
        }),
      )
      .mockResolvedValueOnce(
        credential({ status: "ACTIVE", phone_masked: "138****0000", binding_generation: 1 }),
      );

    renderCard();

    // The binding flow only starts in the modal; the card just offers the entry.
    expect(screen.queryByPlaceholderText("+8613800000000")).not.toBeInTheDocument();
    await openConnectModal();

    // Consent and a valid phone are both required before sending is possible.
    expect(await screen.findByText(CONSENT)).toBeInTheDocument();
    const send = await screen.findByRole("button", { name: "获取验证码" });
    expect(send).toBeDisabled();
    await fillPhoneAndConsent();
    await waitFor(() => expect(buttonByText("获取验证码")).toBeEnabled());

    await click("获取验证码");
    await waitFor(() => expect(startMock).toHaveBeenCalledTimes(1));
    expect(startMock).toHaveBeenCalledWith({
      phone: "+8613800000000",
      consent: true,
      request_id: expect.any(String),
    });

    // The masked phone comes from the server, never from local state.
    expect((await screen.findAllByText(/138\*\*\*\*0000/)).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByPlaceholderText(/位数字/), { target: { value: "87654321" } });
    await click("验证并连接");
    expect(await screen.findByText(/验证码不正确/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/位数字/)).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/位数字/), { target: { value: "12345678" } });
    await click("验证并连接");
    await waitFor(() => expect(verifyMock).toHaveBeenCalledWith("attempt-1", { code: "12345678" }));

    expect(await screen.findByText("已连接（自动续期）")).toBeInTheDocument();
    // 绑定成功后先用本人账号预加载门店目录，缓存完成才收尾。
    expect(await screen.findByText("已缓存 160 家门店数据。")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/位数字/)).not.toBeInTheDocument();
    expect(sessionStorage.getItem("nike_mp_nike_auth_attempt:user-a")).toBeNull();
  });

  it("keeps the connect modal open with a ring while the catalogue preloads", async () => {
    startMock.mockResolvedValue(attempt());
    verifyMock.mockResolvedValue(
      credential({ status: "ACTIVE", phone_masked: "138****0000", binding_generation: 2 }),
    );
    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");
    await waitFor(() => expect(screen.getByPlaceholderText(/位数字/)).toBeInTheDocument());
    bootstrapMock.mockResolvedValue(
      plan({ state: "RUNNING", progress: 0.25, stage: "正在获取门店清单" }),
    );
    fireEvent.change(screen.getByPlaceholderText(/位数字/), { target: { value: "12345678" } });
    await click("验证并连接");

    // 弹窗不关闭，显示环形进度与阶段文案（卡片里也有同文案，按弹窗作用域断言）。
    expect(
      await screen.findByText("正在获取门店清单", { selector: ".ant-modal span" }),
    ).toBeInTheDocument();
    expect(screen.getByText("连接成功，正在用你自己的账号获取并缓存门店数据。")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/位数字/)).not.toBeInTheDocument();

    // 缓存完成后自动收尾。
    bootstrapMock.mockResolvedValue(
      plan({ state: "SUCCEEDED", progress: 1, stage: "门店数据已缓存（160 家）", store_count: 160 }),
    );
    await waitFor(() => expect(screen.getByText("已缓存 160 家门店数据。")).toBeInTheDocument(), {
      timeout: 6000,
    });
  });

  it("offers a retry when the catalogue preload failed", async () => {
    startMock.mockResolvedValue(attempt());
    verifyMock.mockResolvedValue(
      credential({ status: "ACTIVE", phone_masked: "138****0000", binding_generation: 3 }),
    );
    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");
    await waitFor(() => expect(screen.getByPlaceholderText(/位数字/)).toBeInTheDocument());
    bootstrapMock.mockResolvedValue(
      plan({
        state: "FAILED",
        progress: 0.25,
        stage: "未能获取门店数据",
        error_code: "NIKE_UPSTREAM_UNAVAILABLE",
      }),
    );
    fireEvent.change(screen.getByPlaceholderText(/位数字/), { target: { value: "12345678" } });
    await click("验证并连接");

    // 弹窗与卡片都会显示缓存状态，这里按弹窗作用域断言。
    await waitFor(() =>
      expect(
        document.querySelector(".ant-modal .ant-typography-warning, .ant-modal .ant-typography"),
      ).toBeTruthy(),
    );
    expect(await screen.findByText(/门店数据没有获取成功/, { selector: ".ant-modal span" })).toBeInTheDocument();
    startBootstrapMock.mockResolvedValue(
      plan({ state: "PENDING", stage: "排队中：准备获取门店清单" }),
    );
    const retry = document.querySelector<HTMLElement>(".ant-modal .ant-btn");
    expect(retry?.textContent?.replace(/\s+/g, "")).toBe("重新获取");
    await act(async () => {
      fireEvent.click(retry as HTMLElement);
    });
    await waitFor(() => expect(startBootstrapMock).toHaveBeenCalled());
  });

  it("enables resend only after the server resend_after elapses", async () => {
    startMock.mockResolvedValue(
      attempt({ resend_after: new Date(Date.now() + 20000).toISOString() }),
    );

    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");

    await waitFor(() => expect(buttonMatching(/秒后可重新获取/)).not.toBeNull(), { timeout: 5000 });
    const resend = buttonMatching(/秒后可重新获取/);
    expect(resend).not.toBeNull();
    expect(resend as HTMLElement).toBeDisabled();

    await waitFor(() => expect(buttonByText("重新获取验证码")).toBeEnabled(), { timeout: 30000 });
  });

  it("cancels the session and returns to the phone form", async () => {
    startMock.mockResolvedValue(attempt());
    cancelMock.mockResolvedValue(undefined);

    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");
    await waitFor(() => expect(screen.getByPlaceholderText(/位数字/)).toBeInTheDocument());

    await click("取消");
    await waitFor(() => expect(cancelMock).toHaveBeenCalledWith("attempt-1"));
    await waitFor(() => expect(buttonByText("获取验证码")).toBeTruthy());
    expect(screen.queryByPlaceholderText(/位数字/)).not.toBeInTheDocument();
    expect(sessionStorage.getItem("nike_mp_nike_auth_attempt:user-a")).toBeNull();
  });

  it("shows rate-limit seconds from retry_after_seconds without retrying", async () => {
    startMock.mockRejectedValue(
      new ApiError(429, "操作过于频繁", {
        detail: "操作过于频繁",
        error_code: "NIKE_AUTH_RATE_LIMITED",
        retry_after_seconds: 42,
      }),
    );

    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");

    expect(await screen.findByText(/请在 42 秒后重试/)).toBeInTheDocument();
    // 提示里的动作必须真的可以点。
    expect(buttonByText("更换手机号")).not.toBeNull();
  });

  it("names the phone problem instead of pointing at a missing button", async () => {
    // 上游对“该手机号没有 Nike 账号”复用 NIKE_AUTH_REQUEST_CONFLICT；
    // 手机号输入阶段必须给出正确指引，且保持输入框可改。
    startMock.mockRejectedValue(
      new ApiError(409, "该手机号没有 Nike 账号，请先注册或更换手机号", {
        detail: "该手机号没有 Nike 账号，请先注册或更换手机号",
        error_code: "NIKE_AUTH_REQUEST_CONFLICT",
        retry_after_seconds: null,
      }),
    );

    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");

    expect(
      await screen.findByText("这个手机号没有 Nike 账号，请先注册或更换手机号。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/与上一条验证冲突/)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("+8613800000000")).toBeInTheDocument();
    expect(buttonByText("更换手机号")).not.toBeNull();
  });

  it("disables sending when the login switch is off instead of faking success", async () => {
    listMock.mockResolvedValue([
      credential({ status: "NEEDS_AUTHORIZATION", login_available: false }),
    ]);

    renderCard();
    // 已有账号时入口是“再绑一个账号”（connect 模式，不指定 credential_id）。
    await openConnectModal("再绑一个账号");
    expect(await screen.findByText("Nike 短信验证暂未开放")).toBeInTheDocument();

    await fillPhoneAndConsent();
    await waitFor(() => expect(buttonByText("获取验证码")).toBeDisabled());
    await click("获取验证码");
    expect(startMock).not.toHaveBeenCalled();
    expect(screen.queryByPlaceholderText(/位数字/)).not.toBeInTheDocument();
  });

  it("lets a read-only viewer manage their own Nike connection", async () => {
    state.user = { id: "viewer-1" };
    state.canWrite = false;
    startMock.mockResolvedValue(attempt());

    renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await waitFor(() => expect(buttonByText("获取验证码")).toBeEnabled());
    await click("获取验证码");

    expect(startMock).toHaveBeenCalledTimes(1);
    expect((await screen.findAllByText(/138\*\*\*\*0000/)).length).toBeGreaterThan(0);
  });

  it("disconnects the account and refreshes the credential state", async () => {
    listMock.mockResolvedValue([
      credential({ id: "cred-a", status: "ACTIVE", phone_masked: "138****0000", binding_generation: 1 }),
    ]);
    disconnectMock.mockResolvedValue(undefined);

    renderCard();
    expect(await screen.findByText("已连接（自动续期）")).toBeInTheDocument();
    // 自动续期下不展示到期时间，避免误导。
    expect(screen.queryByText("有效期至")).not.toBeInTheDocument();
    expect(screen.getByText("138****0000")).toBeInTheDocument();

    await click("断开连接");
    // Destructive actions require the §2.2 confirmation dialog.
    expect(
      await screen.findByText("断开 Nike 账号连接？", { selector: ".ant-modal-confirm-title" }),
    ).toBeInTheDocument();
    expect(disconnectMock).not.toHaveBeenCalled();
    await clickModalConfirm("断开连接");
    await waitFor(() => expect(disconnectMock).toHaveBeenCalledWith("cred-a"));
  });

  it("lists every bound account and disconnects only the chosen row", async () => {
    listMock.mockResolvedValue([
      credential({ id: "cred-a", status: "ACTIVE", phone_masked: "138****0001", binding_generation: 1 }),
      credential({
        id: "cred-b",
        status: "NEEDS_AUTHORIZATION",
        phone_masked: "139****0002",
        binding_generation: 1,
        cooldown_until: new Date(Date.now() + 1_800_000).toISOString(),
        cooldown_reason: "throttled",
      }),
    ]);
    disconnectMock.mockResolvedValue(undefined);

    renderCard();

    // 两个账号渲染两行，并显示账号总数。
    expect(await screen.findByText("共 2 个账号")).toBeInTheDocument();
    expect(screen.getByText("138****0001")).toBeInTheDocument();
    expect(screen.getByText("139****0002")).toBeInTheDocument();
    // 冷却中的账号只显示恢复时刻（HH:MM），不显示后端原因码。
    expect(screen.getByText(/^冷却至 \d{2}:\d{2}$/)).toBeInTheDocument();
    expect(screen.queryByText("throttled")).not.toBeInTheDocument();

    const disconnectButtons = screen
      .queryAllByRole("button")
      .filter((button) => (button.textContent ?? "").replace(/\s+/g, "") === "断开连接");
    expect(disconnectButtons).toHaveLength(2);
    await act(async () => {
      fireEvent.click(disconnectButtons[1]);
    });

    expect(
      await screen.findByText("断开 Nike 账号连接？", { selector: ".ant-modal-confirm-title" }),
    ).toBeInTheDocument();
    await clickModalConfirm("断开连接");
    await waitFor(() => expect(disconnectMock).toHaveBeenCalledWith("cred-b"));
    expect(disconnectMock).toHaveBeenCalledTimes(1);
  });

  it("opens the reauth flow in the modal and warns about rebuilding the baseline", async () => {
    listMock.mockResolvedValue([
      credential({ id: "cred-a", status: "ACTIVE", phone_masked: "138****0000", binding_generation: 1 }),
    ]);

    renderCard();
    await screen.findByText("已连接（自动续期）");
    await openConnectModal("重新验证/更换账号");

    expect(
      await screen.findByText(
        "旧账号未完成的检查会结束；新账号首次检查只建立基线，不会产生补货或售罄提醒。",
        { selector: ".ant-modal .ant-alert-message" },
      ),
    ).toBeInTheDocument();
    expect(await screen.findByPlaceholderText("+8613800000000")).toBeInTheDocument();
  });

  it("hides the previous user's code entry after an account switch", async () => {
    startMock.mockResolvedValue(attempt());

    const { client, rerender } = renderCard();
    await openConnectModal();
    await fillPhoneAndConsent();
    await click("获取验证码");
    await waitFor(() => expect(screen.getByPlaceholderText(/位数字/)).toBeInTheDocument());
    expect(sessionStorage.getItem("nike_mp_nike_auth_attempt:user-a")).toBe("attempt-1");

    state.user = { id: "user-b" };
    rerender(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AntdApp>
            <NikeCredentialCard />
          </AntdApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.queryByPlaceholderText(/位数字/)).not.toBeInTheDocument());
    await waitFor(
      () => expect(screen.queryByText(/138\*\*\*\*0000/)).not.toBeInTheDocument(),
      { timeout: 6000 },
    );
    await waitFor(() => expect(buttonByText("获取验证码")).toBeTruthy());
  });
});
