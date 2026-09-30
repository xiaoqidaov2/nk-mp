import { Alert, Button, Descriptions, Progress, Space, Table, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeCredentialCard } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { PushChannelsCard } from "@/components/PushChannelsCard";
import { SectionCard } from "@/components/SectionCard";
import { ACCOUNT, BTN, ERR, formatCount } from "@/copy";
import { dashboardApi } from "@/data/api";
import type { Dashboard } from "@/data/types";

const ROLE_LABEL: Record<string, string> = {
  ops_admin: "管理后台管理员",
  ops_operator: "管理后台运营",
  tenant_admin: "公司管理员",
  tenant_viewer: "只读成员",
};

interface CapabilityRow {
  item: string;
  available: string;
}

export function AccountPage() {
  const { user } = useAuth();
  const dashboard = useQuery<Dashboard>({ queryKey: ["dashboard"], queryFn: () => dashboardApi.get() });

  const used = dashboard.data?.review_requests_today ?? 0;
  const budget = dashboard.data?.daily_review_budget ?? 0;
  const percent = budget > 0 ? Math.min(100, Math.round((used / budget) * 100)) : 0;

  const rows = ACCOUNT.capabilityRows as readonly CapabilityRow[];

  const capabilityList = (
    <ResponsiveList<CapabilityRow>
      items={[...rows]}
      rowKey={(row) => row.item}
      renderCard={(row) => (
        <>
          <CardRow label="数据">{row.item}</CardRow>
          <CardRow label="说明">{row.available}</CardRow>
        </>
      )}
      table={
        <Table<CapabilityRow>
          size="small"
          rowKey="item"
          pagination={false}
          dataSource={[...rows]}
          columns={[
            { title: "数据", dataIndex: "item" },
            { title: "说明", dataIndex: "available" },
          ]}
        />
      }
    />
  );

  const role = user?.role ?? "";

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="账户与额度" />

      {dashboard.isError ? (
        <Alert
          type="error"
          showIcon
          message={ERR.accountFailed}
          description={ERR.accountFailedHint}
          action={
            <Button size="small" onClick={() => void dashboard.refetch()}>
              {BTN.refresh}
            </Button>
          }
        />
      ) : null}

      <SectionCard title="当前账号">
        <Descriptions size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="邮箱">{user?.email}</Descriptions.Item>
          <Descriptions.Item label="显示名称">{user?.display_name || "—"}</Descriptions.Item>
          <Descriptions.Item label={ACCOUNT.roleLabel}>
            <Space direction="vertical" size={2}>
              <Tag>{ROLE_LABEL[role] ?? role}</Tag>
              <Typography.Text type="secondary">
                {ACCOUNT.roleDescriptions[role] ?? ""}
              </Typography.Text>
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label="所属公司">
            {user?.tenant_name}（{user?.tenant_slug}）
          </Descriptions.Item>
        </Descriptions>
      </SectionCard>

      <NikeCredentialCard />

      <PushChannelsCard />

      <SectionCard title={ACCOUNT.quotaTitle}>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Progress percent={percent} status={percent > 90 ? "exception" : "normal"} />
          <Typography.Text>
            {ACCOUNT.quotaUsed(formatCount(used), formatCount(budget))}
          </Typography.Text>
          <Typography.Text type="secondary">{ACCOUNT.quotaNote}</Typography.Text>
        </Space>
      </SectionCard>

      <SectionCard title={ACCOUNT.capabilityTitle}>{capabilityList}</SectionCard>
    </Space>
  );
}
