import { Alert, App as AntdApp, Button, Descriptions, Space, Tag } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { BTN, ERR, MSG, MODALS, formatDateTimeSeconds } from "@/copy";
import { nikeCredentialApi } from "@/data/api";
import type { NikeCredentialOut, NikeCredentialStatus } from "@/data/types";
import { useBootstrapQuery } from "@/hooks/useBootstrapQuery";

import { CatalogProgress } from "./CatalogProgress";
import { NikeConnectModal } from "./NikeConnectModal";
import { SectionCard } from "./SectionCard";
import { StatusPill } from "./StatusPill";

const CARD_SUBTITLE = "独立第三方，非 Nike 官方";
const CARD_DECLARATION =
  "本平台不是 Nike 官方页面，也不会要求你输入 Nike 密码。只通过短信验证码获得只读授权，并加密保存续期凭证。";

/** ACTIVE/EXPIRING are the only states that authorise personal collection. */
export function isCredentialActive(status: NikeCredentialStatus | undefined): boolean {
  return status === "ACTIVE" || status === "EXPIRING";
}

export function useNikeCredentialQuery() {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  return useQuery<NikeCredentialOut>({
    queryKey: ["nike-credential", userId],
    queryFn: () => nikeCredentialApi.status(),
    enabled: Boolean(userId),
    retry: false,
    staleTime: 15_000,
  });
}

/** Drops cached data derived from the previous binding so pages refetch. */
function invalidatePersonalData(client: QueryClient): void {
  for (const queryKey of [
    ["matrix"],
    ["changes"],
    ["dashboard"],
    ["tasks"],
    ["scans"],
    ["scan"],
  ]) {
    void client.cancelQueries({ queryKey });
    void client.invalidateQueries({ queryKey });
  }
}

/**
 * Unified "connect first" hint used by every collection entry point. The
 * backend still rejects unbound requests independently.
 */
export function NikeConnectionNotice({ action }: { action: string }) {
  const credential = useNikeCredentialQuery();
  if (!credential.data || isCredentialActive(credential.data.status)) return null;
  return (
    <Alert
      type="warning"
      showIcon
      message={`需要先连接 Nike 账号才能${action}。`}
      description={
        <span>
          请到<Link to="/account">账户与额度页</Link>用本人手机号完成验证；未连接或验证失效时后端也会拒绝读取。
        </span>
      }
    />
  );
}

/**
 * 连接状态卡：只展示状态与入口动作，绑定/重新验证的输入流程在弹窗内完成。
 */
export function NikeCredentialCard() {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  const queryClient = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const credential = useNikeCredentialQuery();
  const bootstrap = useBootstrapQuery();
  const [connectOpen, setConnectOpen] = useState(false);
  const [mode, setMode] = useState<"connect" | "reauth">("connect");

  const disconnect = useMutation({
    mutationFn: () => nikeCredentialApi.disconnect(),
    onSuccess: () => {
      message.success(MSG.disconnected());
      void queryClient.invalidateQueries({ queryKey: ["nike-credential", userId] });
      invalidatePersonalData(queryClient);
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const confirmDisconnect = () => {
    const tpl = MODALS.disconnectNike();
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      okButtonProps: { danger: true },
      onOk: () => disconnect.mutate(),
    });
  };

  const openConnect = (nextMode: "connect" | "reauth") => {
    setMode(nextMode);
    setConnectOpen(true);
  };

  const status = credential.data?.status ?? "NOT_CONNECTED";
  const active = isCredentialActive(status);
  const plan = bootstrap.data;
  const retryBootstrap = () => {
    void nikeCredentialApi.startBootstrap().then(() => bootstrap.refetch());
  };

  return (
    <SectionCard title="Nike 账号连接" extra={<Tag color="default">{CARD_SUBTITLE}</Tag>}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert type="info" showIcon message={CARD_SUBTITLE} description={CARD_DECLARATION} />

        {plan && plan.state !== "NONE" ? (
          <CatalogProgress plan={plan} compact onRetry={retryBootstrap} />
        ) : null}

        {credential.isLoading ? null : active ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions size="small" column={{ xs: 1, md: 2 }}>
              <Descriptions.Item label="状态">
                <StatusPill domain="credential" value={status} />
              </Descriptions.Item>
              <Descriptions.Item label="已连接手机号">
                {credential.data?.phone_masked || "—"}
              </Descriptions.Item>
              <Descriptions.Item label="有效期至">
                {credential.data?.access_expires_at
                  ? formatDateTimeSeconds(credential.data.access_expires_at)
                  : "—"}
              </Descriptions.Item>
              <Descriptions.Item label="最近续期">
                {credential.data?.last_refreshed_at
                  ? formatDateTimeSeconds(credential.data.last_refreshed_at)
                  : "—"}
              </Descriptions.Item>
            </Descriptions>
            <Alert type="warning" showIcon message={MODALS.changeAccount().content} />
            <Space wrap>
              <Button
                type="primary"
                className="touch-target"
                onClick={() => openConnect("reauth")}
              >
                {BTN.reauth}
              </Button>
              <Button
                danger
                className="touch-target"
                loading={disconnect.isPending}
                onClick={confirmDisconnect}
              >
                {BTN.disconnect}
              </Button>
            </Space>
          </Space>
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="状态">
                <StatusPill domain="credential" value={status} />
              </Descriptions.Item>
            </Descriptions>
            {status === "NEEDS_AUTHORIZATION" || status === "REVOKED" ? (
              <Alert
                type="warning"
                showIcon
                message="当前 Nike 连接不可用，需要重新验证后才能继续读取数据。"
              />
            ) : null}
            <Space wrap>
              <Button
                type="primary"
                className="touch-target"
                onClick={() => openConnect("connect")}
              >
                {BTN.connectNike}
              </Button>
            </Space>
          </Space>
        )}

        <NikeConnectModal
          open={connectOpen}
          mode={mode}
          onClose={() => setConnectOpen(false)}
          loginAvailable={credential.data?.login_available ?? false}
          credentialLoading={credential.isLoading}
          onConnected={() => invalidatePersonalData(queryClient)}
        />
      </Space>
    </SectionCard>
  );
}
