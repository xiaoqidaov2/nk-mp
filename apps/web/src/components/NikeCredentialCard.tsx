import { Alert, App as AntdApp, Button, Descriptions, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { ACCOUNT, BTN, ERR, MSG, MODALS, formatClock } from "@/copy";
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
const CARD_RENEWAL_NOTE = "登录状态到期前会自动续期，无需重新验证。";

/** ACTIVE/EXPIRING are the only states that authorise personal collection. */
export function isCredentialActive(status: NikeCredentialStatus | undefined): boolean {
  return status === "ACTIVE" || status === "EXPIRING";
}

/** 本人已绑定的全部 Nike 账号；空数组＝未连接。 */
export function useNikeCredentialsQuery() {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  return useQuery<NikeCredentialOut[]>({
    queryKey: ["nike-credentials", userId],
    queryFn: () => nikeCredentialApi.list(),
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
  const credentials = useNikeCredentialsQuery();
  const rows = credentials.data;
  // 未拿到数据前不提示；有一个可用账号即视为已连接。
  if (!rows || rows.some((row) => isCredentialActive(row.status))) return null;
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
 * 连接状态卡：列出本人已绑定的每个 Nike 账号，逐个可重新验证或断开；
 * 绑定/重新验证的输入流程在弹窗内完成。
 */
export function NikeCredentialCard() {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  const queryClient = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const credentials = useNikeCredentialsQuery();
  const bootstrap = useBootstrapQuery();
  const [connectOpen, setConnectOpen] = useState(false);
  const [mode, setMode] = useState<"connect" | "reauth">("connect");
  const [targetId, setTargetId] = useState<string | undefined>(undefined);

  const rows = credentials.data ?? [];

  const disconnect = useMutation({
    mutationFn: (credentialId: string) => nikeCredentialApi.disconnect(credentialId),
    onSuccess: () => {
      message.success(MSG.disconnected());
      void queryClient.invalidateQueries({ queryKey: ["nike-credentials", userId] });
      invalidatePersonalData(queryClient);
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const confirmDisconnect = (credentialId: string) => {
    const tpl = MODALS.disconnectNike();
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      okButtonProps: { danger: true },
      onOk: () => disconnect.mutateAsync(credentialId),
    });
  };

  const openConnect = (nextMode: "connect" | "reauth", credentialId?: string) => {
    setMode(nextMode);
    setTargetId(credentialId);
    setConnectOpen(true);
  };

  const closeConnect = () => {
    setConnectOpen(false);
    setTargetId(undefined);
  };

  const plan = bootstrap.data;
  const retryBootstrap = () => {
    void nikeCredentialApi.startBootstrap().then(() => bootstrap.refetch());
  };
  // 列表为空时没有账号可读短信开关：默认开放，真实限制由服务端提交时兜底。
  const loginAvailable = rows.length === 0 || rows.some((row) => row.login_available);

  return (
    <SectionCard title={ACCOUNT.listTitle} extra={<Tag color="default">{CARD_SUBTITLE}</Tag>}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert type="info" showIcon message={CARD_SUBTITLE} description={CARD_DECLARATION} />

        {plan && plan.state !== "NONE" ? (
          <CatalogProgress plan={plan} compact onRetry={retryBootstrap} />
        ) : null}

        {credentials.isLoading ? null : rows.length > 0 ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Typography.Text type="secondary">{ACCOUNT.count(rows.length)}</Typography.Text>
            {rows.some((row) => row.status === "NEEDS_AUTHORIZATION") ? (
              <Alert
                type="warning"
                showIcon
                message="有账号需要重新验证后才能继续读取数据。"
              />
            ) : null}
            <Alert type="warning" showIcon message={MODALS.changeAccount().content} />
            <Table<NikeCredentialOut>
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={rows}
              columns={[
                {
                  title: ACCOUNT.colStatus,
                  dataIndex: "status",
                  width: 170,
                  render: (value: NikeCredentialStatus) => (
                    <StatusPill domain="credential" value={value} />
                  ),
                },
                {
                  title: ACCOUNT.colPhone,
                  dataIndex: "phone_masked",
                  render: (value: string | null) => value || "—",
                },
                {
                  title: ACCOUNT.colCooldown,
                  dataIndex: "cooldown_until",
                  width: 130,
                  render: (_: string | null, row) => {
                    // 只有还没到恢复时间才提示；过期残留值不显示。
                    if (!row.cooldown_until) return "—";
                    const until = Date.parse(row.cooldown_until);
                    if (Number.isNaN(until) || until <= Date.now()) return "—";
                    return ACCOUNT.coolingUntil(formatClock(row.cooldown_until));
                  },
                },
                {
                  title: ACCOUNT.colActions,
                  key: "actions",
                  width: 210,
                  render: (_: unknown, row) => (
                    <Space size={8} wrap>
                      <Button size="small" onClick={() => openConnect("reauth", row.id)}>
                        {BTN.reauth}
                      </Button>
                      <Button
                        size="small"
                        danger
                        loading={disconnect.isPending && disconnect.variables === row.id}
                        onClick={() => confirmDisconnect(row.id)}
                      >
                        {BTN.disconnect}
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
            <Typography.Text type="secondary">{CARD_RENEWAL_NOTE}</Typography.Text>
            <Space wrap>
              <Button
                type="primary"
                className="touch-target"
                onClick={() => openConnect("connect")}
              >
                {ACCOUNT.addAnother}
              </Button>
            </Space>
          </Space>
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label={ACCOUNT.colStatus}>
                <StatusPill domain="credential" value="NOT_CONNECTED" />
              </Descriptions.Item>
            </Descriptions>
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
          credentialId={targetId}
          onClose={closeConnect}
          loginAvailable={loginAvailable}
          credentialLoading={credentials.isLoading}
          onConnected={() => invalidatePersonalData(queryClient)}
        />
      </Space>
    </SectionCard>
  );
}
