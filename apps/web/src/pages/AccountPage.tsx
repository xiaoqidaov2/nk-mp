import { Alert, Card, Descriptions, Progress, Space, Table, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";

import { dashboardApi, opsApi } from "@/data/api";
import type { CredentialStatus, Dashboard } from "@/data/types";
import { useAuth } from "@/auth/AuthContext";

const ROLE_LABEL: Record<string, string> = {
  ops_admin: "服务方管理员",
  ops_operator: "服务方运营",
  tenant_admin: "客户管理员",
  tenant_viewer: "客户查看者",
};

function credentialText(status: CredentialStatus | undefined): string {
  if (!status) return "未知";
  if (status.status === "ACTIVE") return "正常";
  if (status.status === "EXPIRING") return "即将到期";
  return "需要重新授权";
}

export function AccountPage() {
  const { user, isOps } = useAuth();
  const dashboard = useQuery<Dashboard>({ queryKey: ["dashboard"], queryFn: () => dashboardApi.get() });
  const credential = useQuery<CredentialStatus>({
    queryKey: ["credential"],
    queryFn: () => opsApi.credential(),
    enabled: isOps,
    retry: false,
  });

  const used = dashboard.data?.review_requests_today ?? 0;
  const budget = dashboard.data?.daily_review_budget ?? 0;
  const percent = budget > 0 ? Math.min(100, Math.round((used / budget) * 100)) : 0;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small" title="当前账号">
        <Descriptions size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="邮箱">{user?.email}</Descriptions.Item>
          <Descriptions.Item label="显示名称">{user?.display_name || "—"}</Descriptions.Item>
          <Descriptions.Item label="角色">
            <Tag>{ROLE_LABEL[user?.role ?? ""] ?? user?.role}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="所属租户">
            {user?.tenant_name}（{user?.tenant_slug}）
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card size="small" title="今日审核配额">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Progress percent={percent} status={percent > 90 ? "exception" : "normal"} />
          <Typography.Text>
            已使用 {used} / {budget} 次审核请求
          </Typography.Text>
          <Typography.Text type="secondary">
            配额同时受账号风控约束；接近上限时系统会优先保证已订阅货号的可用性检查，价格可能显示“待补价”。
          </Typography.Text>
        </Space>
      </Card>

      {isOps ? (
        <Card size="small" title="采集凭证状态">
          {credential.isError ? (
            <Alert type="warning" showIcon message="无法读取凭证状态（需要服务方角色）" />
          ) : (
            <Descriptions size="small" column={{ xs: 1, md: 2 }}>
              <Descriptions.Item label="状态">
                <Tag color={credential.data?.status === "ACTIVE" ? "green" : "orange"}>
                  {credentialText(credential.data)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Access Token 到期">
                {credential.data?.access_expires_at
                  ? new Date(credential.data.access_expires_at).toLocaleString("zh-CN")
                  : "—"}
              </Descriptions.Item>
              <Descriptions.Item label="最近续期">
                {credential.data?.last_refreshed_at
                  ? new Date(credential.data.last_refreshed_at).toLocaleString("zh-CN")
                  : "—"}
              </Descriptions.Item>
              <Descriptions.Item label="最近错误">
                {credential.data?.last_error || "—"}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Card>
      ) : null}

      <Card size="small" title="数据边界说明">
        <Table
          size="small"
          rowKey="item"
          pagination={false}
          dataSource={[
            { item: "全国门店清单", available: "可获取（含地址与变更）" },
            { item: "指定货号的门店尺码库存", available: "可获取（最近探测，非实时）" },
            { item: "门店价与促销后实付价", available: "可获取（缺货时无价格）" },
            { item: "单店全量在售商品与件数", available: "不可获取，页面不提供该口径" },
          ]}
          columns={[
            { title: "数据能力", dataIndex: "item" },
            { title: "说明", dataIndex: "available" },
          ]}
        />
      </Card>
    </Space>
  );
}
