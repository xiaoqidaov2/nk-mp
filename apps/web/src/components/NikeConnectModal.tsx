import {
  Alert,
  App as AntdApp,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Typography,
} from "antd";
import type { InputRef } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useAuth } from "@/auth/AuthContext";
import { BTN, CATALOG, ERR, MODALS, MSG, formatCountdown, resendLabel } from "@/copy";
import { nikeCredentialApi } from "@/data/api";
import { ApiError } from "@/data/client";
import { clearNikeAttemptId, readNikeAttemptId, writeNikeAttemptId } from "@/data/nikeAttempt";
import type { NikeAuthAttemptOut, NikeCredentialOut } from "@/data/types";

import { CatalogProgress } from "./CatalogProgress";
import { errorCodeText } from "./StatusTag";
import { useBootstrapQuery } from "@/hooks/useBootstrapQuery";

/** 上游契约锁定验证码格式，这里只限宽，不硬编码服务器长度。 */
const CODE_MIN_LENGTH = 4;
const CODE_MAX_LENGTH = 8;
const PHONE_PREFIX = "+86";
export const CONSENT_TEXT = "我同意本平台使用我的 Nike 账号进行只读采集并保存加密续期凭证";
const DECLARATION =
  "本平台不是 Nike 官方页面，也不会要求你输入 Nike 密码。只通过短信验证码获得只读授权，并加密保存续期凭证。";

/**
 * 手机号输入阶段的错误文案覆盖：上游把“该手机号没有 Nike 账号”映射为
 * NIKE_AUTH_REQUEST_CONFLICT，此时“与上一条验证冲突、请点更换手机号”是错误指引。
 */
function errorText(error: unknown, phoneStage: boolean): string {
  if (error instanceof ApiError) {
    // 原始 detail 只进控制台，界面永不展示。
    console.error(error);
    if (phoneStage && error.errorCode === "NIKE_AUTH_REQUEST_CONFLICT") {
      return "这个手机号没有 Nike 账号，请先注册或更换手机号。";
    }
    return errorCodeText(error.errorCode, error.retryAfterSeconds) ?? ERR.commonFailure;
  }
  if (error instanceof Error) console.error(error);
  return ERR.network;
}

/** Normalises a mainland phone number to E.164, or returns null when invalid. */
function normalizePhone(input: string): string | null {
  const digits = input.replace(/\D/g, "");
  const local = digits.length === 13 && digits.startsWith("86") ? digits.slice(2) : digits;
  return /^1[3-9]\d{9}$/.test(local) ? `${PHONE_PREFIX}${local}` : null;
}

function secondsUntil(iso: string | null | undefined, now: number): number {
  if (!iso) return 0;
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.ceil((target - now) / 1000));
}

function newRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export interface NikeConnectModalProps {
  open: boolean;
  /** connect：未连接时首次绑定；reauth：已连接时更换/重新验证。 */
  mode: "connect" | "reauth";
  onClose: () => void;
  /** 上游短信通道是否开放（来自连接状态）。 */
  loginAvailable: boolean;
  credentialLoading: boolean;
  /** 连接成功后的数据刷新（凭据 + 依赖此连接的数据）。 */
  onConnected: () => void;
}

/**
 * Nike 账号绑定流程（手机号 → 短信验证码 → 连接）。
 * 仅在弹窗内进行输入；关闭弹窗不取消已发送的验证码（sessionStorage 里保留会话，可继续）。
 */
export function NikeConnectModal({
  open,
  mode,
  onClose,
  loginAvailable,
  credentialLoading,
  onConnected,
}: NikeConnectModalProps) {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  const queryClient = useQueryClient();
  const { message } = AntdApp.useApp();

  const [phone, setPhone] = useState(PHONE_PREFIX);
  const [consent, setConsent] = useState(false);
  const phoneRef = useRef<InputRef>(null);
  const [code, setCode] = useState("");
  const [attemptRef, setAttemptRef] = useState<{ userId: string; id: string | null }>(() => ({
    userId,
    id: userId ? readNikeAttemptId(userId) : null,
  }));
  const [notice, setNotice] = useState("");
  const [now, setNow] = useState(() => Date.now());
  // 绑定成功后停留在弹窗内展示预加载进度（环形）。
  const [bootstrapping, setBootstrapping] = useState(false);
  const bootstrap = useBootstrapQuery(open);

  // 存储的 attempt id 只属于创建它的账号：切号后同一渲染内即失效。
  const attemptId = attemptRef.userId === userId ? attemptRef.id : null;

  useEffect(() => {
    if (!open) return;
    setAttemptRef({ userId, id: userId ? readNikeAttemptId(userId) : null });
    setPhone(PHONE_PREFIX);
    setConsent(false);
    setCode("");
    setNotice("");
    setNow(Date.now());
  }, [open, userId]);

  const attemptKey = attemptId ?? "";
  const attempt = useQuery<NikeAuthAttemptOut>({
    queryKey: ["nike-auth-attempt", userId, attemptKey],
    queryFn: () => nikeCredentialApi.attempt(attemptKey),
    enabled: Boolean(open && userId && attemptKey),
    retry: false,
    refetchInterval: (query) => (query.state.data?.state === "SENDING" ? 3000 : false),
  });

  const attemptState = attempt.data?.state;
  const restoringAttempt = Boolean(attemptKey) && attempt.isLoading;
  const inVerification =
    Boolean(attemptKey) &&
    (attemptState === "SENDING" || attemptState === "CODE_SENT" || attemptState === "VERIFYING");

  const resendIn = secondsUntil(attempt.data?.resend_after, now);
  const expiresIn = secondsUntil(attempt.data?.expires_at, now);
  const codeExpired = Boolean(attempt.data?.expires_at) && expiresIn <= 0;
  const attemptsRemaining = attempt.data?.attempts_remaining ?? null;
  // 手机号表单出现时（首次打开 / 更换手机号 / 取消后）把焦点交给输入框。
  useEffect(() => {
    if (open && !inVerification && !restoringAttempt) phoneRef.current?.focus();
  }, [open, inVerification, restoringAttempt]);

  const needsTick = inVerification && Boolean(attempt.data?.resend_after || attempt.data?.expires_at);
  useEffect(() => {
    if (!needsTick) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [needsTick]);

  const plan = bootstrap.data;
  const planning = plan?.state === "PENDING" || plan?.state === "RUNNING";
  const showCatalog = bootstrapping || planning;

  /** 预加载完成即关闭弹窗并提示缓存结果。 */
  useEffect(() => {
    if (!bootstrapping || plan?.state !== "SUCCEEDED") return;
    message.success(MSG.catalogCached(plan.store_count));
    setBootstrapping(false);
    onClose();
  }, [bootstrapping, plan, message, onClose]);

  const retryBootstrap = () => {
    void nikeCredentialApi.startBootstrap().then(() => bootstrap.refetch());
  };

  const finishConnected = (updated?: NikeCredentialOut) => {
    if (userId) {
      clearNikeAttemptId(userId);
      // 本标签页验证成功时响应里就是最新凭据，直接落缓存；跨标签页完成才需要重取。
      if (updated) queryClient.setQueryData(["nike-credential", userId], updated);
      else void queryClient.invalidateQueries({ queryKey: ["nike-credential", userId] });
    }
    onConnected();
    message.success(MSG.connected());
    onClose();
  };

  // 其他标签页完成的验证在这里对账，不会把已连接的流程留在验证码界面。
  useEffect(() => {
    const state = attempt.data?.state;
    if (!open || !userId || !state) return;
    if (state === "CONNECTED") {
      finishConnected();
      return;
    }
    if (
      state === "FAILED" ||
      state === "EXPIRED" ||
      state === "CANCELLED" ||
      state === "CHALLENGE_REQUIRED"
    ) {
      clearNikeAttemptId(userId);
      setAttemptRef({ userId, id: null });
      setCode("");
      setNotice(
        errorCodeText(attempt.data?.error_code ?? null, null) ??
          "本次验证未完成，请重新获取验证码。",
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt.data, userId, open]);

  const start = useMutation({
    mutationFn: (target: string) =>
      nikeCredentialApi.start({ phone: target, consent: true, request_id: newRequestId() }),
    onSuccess: (created) => {
      setNotice("");
      setNow(Date.now());
      setCode("");
      if (created.phone_masked) {
        message.success(
          MSG.codeSent(
            created.phone_masked,
            formatCountdown(secondsUntil(created.expires_at, Date.now())),
          ),
        );
      }
      if (userId) {
        writeNikeAttemptId(userId, created.id);
        setAttemptRef({ userId, id: created.id });
        queryClient.setQueryData(["nike-auth-attempt", userId, created.id], created);
      }
    },
    onError: (error) => setNotice(errorText(error, true)),
  });

  const verify = useMutation({
    mutationFn: () => nikeCredentialApi.verify(attemptKey, { code: code.trim() }),
    onSuccess: (updated) => {
      setCode("");
      setPhone(PHONE_PREFIX);
      setConsent(false);
      setNotice("");
      if (userId) {
        clearNikeAttemptId(userId);
        queryClient.setQueryData(["nike-credential", userId], updated);
      }
      onConnected();
      setBootstrapping(true);
      void queryClient.invalidateQueries({ queryKey: ["nike-bootstrap", userId] });
    },
    onError: (error) => {
      setNotice(errorText(error, false));
      if (attemptKey) {
        void queryClient.invalidateQueries({ queryKey: ["nike-auth-attempt", userId, attemptKey] });
      }
    },
  });

  const cancel = useMutation({
    mutationFn: () => nikeCredentialApi.cancel(attemptKey),
    onSettled: () => {
      if (userId) clearNikeAttemptId(userId);
      setAttemptRef({ userId, id: null });
      setCode("");
    },
    onError: (error) => setNotice(errorText(error, false)),
  });

  const normalizedPhone = normalizePhone(phone);
  const canSendCode = Boolean(normalizedPhone) && consent && loginAvailable && !start.isPending;
  const canVerify =
    attemptState === "CODE_SENT" &&
    code.trim().length >= CODE_MIN_LENGTH &&
    !codeExpired &&
    attemptsRemaining !== 0 &&
    !verify.isPending;
  const canResend = resendIn <= 0 && Boolean(normalizedPhone) && !start.isPending;

  /** 更换手机号：清空输入（表单出现后自动聚焦）；有会话时真正取消，新号码才能立刻重新获取验证码。 */
  const changePhone = () => {
    setNotice("");
    setCode("");
    setPhone(PHONE_PREFIX);
    if (attemptKey) {
      cancel.mutate(undefined, {
        onSettled: () => {
          if (userId) clearNikeAttemptId(userId);
          setAttemptRef({ userId, id: null });
        },
      });
      return;
    }
    if (userId) clearNikeAttemptId(userId);
    setAttemptRef({ userId, id: null });
  };

  const codeHint = codeExpired
    ? "验证码已过期，请重新获取。"
    : attemptsRemaining === 0
      ? "本次已用完全部验证机会，请重新获取验证码。"
      : attemptsRemaining !== null
        ? `剩余 ${attemptsRemaining} 次验证机会。`
        : "验证码由短信发送到上方手机号。";

  return (
    <Modal
      open={open}
      title={mode === "reauth" ? "重新验证 Nike 账号" : "连接 Nike 账号"}
      footer={null}
      onCancel={onClose}
      afterOpenChange={(opened) => {
        // 弹窗自身的自动聚焦在动画后才会落到容器上，这里把焦点交给手机号输入框。
        if (opened) phoneRef.current?.focus();
      }}
      destroyOnHidden
      maskClosable={false}
      width={480}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert type="info" showIcon message={DECLARATION} />

        {mode === "reauth" ? (
          <Alert type="warning" showIcon message={MODALS.changeAccount().content} />
        ) : null}

        {notice ? (
          <Alert
            type="error"
            showIcon
            message={notice}
            action={
              <Button size="small" onClick={changePhone}>
                {BTN.changePhone}
              </Button>
            }
          />
        ) : null}

        {showCatalog ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {CATALOG.afterConnect}
            </Typography.Paragraph>
            <CatalogProgress plan={plan ?? { state: "PENDING", progress: 0.02, stage: CATALOG.loading, store_count: 0, error_code: null, updated_at: null }} onRetry={retryBootstrap} />
          </Space>
        ) : null}

        {credentialLoading ? <Spin /> : null}

        {restoringAttempt ? (
          <Space>
            <Spin size="small" />
            <Typography.Text type="secondary">正在确认验证会话…</Typography.Text>
          </Space>
        ) : null}

        {!showCatalog && !inVerification && !restoringAttempt ? (
          <Form layout="vertical" onSubmitCapture={(event) => event.preventDefault()}>
            <Form.Item
              label="Nike 账号手机号"
              extra="中国大陆手机号，验证码会发到这个号码。"
            >
              <Input
                ref={phoneRef}
                value={phone}
                onChange={(event) => setPhone(event.target.value)}
                placeholder="+8613800000000"
                maxLength={16}
                autoComplete="tel"
              />
            </Form.Item>
            <Form.Item>
              <Checkbox checked={consent} onChange={(event) => setConsent(event.target.checked)}>
                {CONSENT_TEXT}
              </Checkbox>
            </Form.Item>
            {!loginAvailable ? (
              <Alert
                type="warning"
                showIcon
                message="Nike 短信验证暂未开放"
                description="无法发送验证码；已连接账号的自动续期与断开不受影响。"
                style={{ marginBottom: 12 }}
              />
            ) : null}
            <Space wrap>
              <Button
                type="primary"
                className="touch-target"
                disabled={!canSendCode}
                loading={start.isPending}
                onClick={() => {
                  if (normalizedPhone) start.mutate(normalizedPhone);
                }}
              >
                {BTN.getCode}
              </Button>
              <Button className="touch-target" onClick={onClose}>
                {BTN.cancel}
              </Button>
            </Space>
            {!consent ? (
              <div style={{ marginTop: 8 }}>
                <Typography.Text type="secondary">
                  请先勾选上面的授权说明，再获取验证码。
                </Typography.Text>
              </div>
            ) : null}
          </Form>
        ) : null}

        {!showCatalog && inVerification ? (
          <Form layout="vertical" onSubmitCapture={(event) => event.preventDefault()}>
            {attemptState === "SENDING" ? (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Space>
                  <Spin size="small" />
                  <Typography.Text type="secondary">正在发送验证码…</Typography.Text>
                </Space>
                <Button loading={cancel.isPending} onClick={() => cancel.mutate()}>
                  {BTN.cancel}
                </Button>
              </Space>
            ) : (
              <>
                <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                  {codeExpired
                    ? "验证码已过期，请重新获取。"
                    : `验证码已发送到 ${attempt.data?.phone_masked || "—"}，${formatCountdown(expiresIn)} 内有效。`}
                </Typography.Paragraph>
                <Form.Item label="短信验证码" extra={codeHint}>
                  <Input
                    value={code}
                    onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
                    maxLength={CODE_MAX_LENGTH}
                    inputMode="numeric"
                    placeholder={`${CODE_MIN_LENGTH}-${CODE_MAX_LENGTH} 位数字`}
                    autoComplete="one-time-code"
                  />
                </Form.Item>
                <Space wrap>
                  <Button
                    type="primary"
                    className="touch-target"
                    loading={verify.isPending}
                    disabled={!canVerify}
                    onClick={() => verify.mutate()}
                  >
                    {BTN.verifyConnect}
                  </Button>
                  <Button className="touch-target" loading={cancel.isPending} onClick={changePhone}>
                    {BTN.changePhone}
                  </Button>
                  <Button
                    className="touch-target"
                    disabled={cancel.isPending}
                    onClick={() =>
                      cancel.mutate(undefined, {
                        onSettled: () => {
                          if (userId) clearNikeAttemptId(userId);
                          setAttemptRef({ userId, id: null });
                          setCode("");
                        },
                      })
                    }
                  >
                    {BTN.cancel}
                  </Button>
                  <Button
                    type="link"
                    size="small"
                    disabled={!canResend}
                    loading={start.isPending}
                    onClick={() => {
                      if (normalizedPhone) start.mutate(normalizedPhone);
                    }}
                  >
                    {resendLabel(resendIn)}
                  </Button>
                </Space>
                {resendIn <= 0 && !normalizedPhone ? (
                  <Typography.Text type="secondary">
                    页面已刷新，请点击“更换手机号”重新输入手机号获取验证码。
                  </Typography.Text>
                ) : null}
              </>
            )}
          </Form>
        ) : null}
      </Space>
    </Modal>
  );
}
