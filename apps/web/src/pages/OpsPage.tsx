import { Alert, App as AntdApp, Button, Descriptions, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice, NikeCredentialCard, isCredentialActive, useNikeCredentialQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { BTN, EMPTY, ERR, MODALS, MSG, OPS, formatCount } from "@/copy";
import { dashboardApi, opsApi } from "@/data/api";
import { opApiList } from "@/data/opsTypes";
import type { TenantRow } from "@/data/opsTypes";

export function OpsPage() {
  const queryClient = useQueryClient();
  const credential = useNikeCredentialQuery();
  const bound = isCredentialActive(credential.data?.status);
  const { message, modal } = AntdApp.useApp();
  const tenants = useQuery({
    queryKey: ["ops", "tenants"],
    queryFn: () => opApiList(),
  });
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.get() });
  const health = useQuery({
    queryKey: ["ops", "health"],
    queryFn: () => opsApi.healthDetail(),
    retry: false,
  });
  const upstream = health.data?.nike_api_base ?? "";
  // 本地假服务意味着这不是真实 Nike：文案必须说实话。
  const offline = /(127\.0\.0\.1|localhost|:\/\/stub)/.test(upstream);

  const sync = useMutation({
    mutationFn: () => opsApi.syncStores(),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["stores"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      message.success(MSG.storesSynced(result.synced ?? 0));
    },
  });

  const confirmSync = () => {
    const tpl = MODALS.syncStores();
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      onOk: () => sync.mutate(),
    });
  };

  const tenantCards = (
    <ResponsiveList<TenantRow>
      items={tenants.data ?? []}
      rowKey={(row) => row.id}
      loading={tenants.isLoading}
      empty={<div className="empty-state__description">{EMPTY.opsTenants}</div>}
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <span>{row.name}</span>
            <Tag className={row.is_platform ? "tenant-tag--platform" : "tenant-tag--customer"}>
              {row.is_platform ? OPS.tenantTypePlatform : OPS.tenantTypeCustomer}
            </Tag>
          </div>
          <CardRow label="公司标识">
            <Typography.Text code>{row.slug}</Typography.Text>
          </CardRow>
          <CardRow label="监控任务数">{formatCount(row.task_count)}</CardRow>
          <CardRow label="日额度">{`${formatCount(row.daily_review_budget)} 次`}</CardRow>
        </>
      )}
      table={
        <Table<TenantRow>
          size="small"
          rowKey="id"
          loading={tenants.isLoading}
          dataSource={tenants.data ?? []}
          pagination={false}
          locale={{ emptyText: EMPTY.opsTenants }}
          columns={[
            { title: "公司名称", dataIndex: "name" },
            {
              title: "公司标识",
              dataIndex: "slug",
              width: 140,
              render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
            },
            {
              title: "类型",
              dataIndex: "is_platform",
              width: 110,
              render: (value: boolean) => (
                <Tag className={value ? "tenant-tag--platform" : "tenant-tag--customer"}>
                  {value ? OPS.tenantTypePlatform : OPS.tenantTypeCustomer}
                </Tag>
              ),
            },
            {
              title: "监控任务数",
              dataIndex: "task_count",
              width: 100,
              render: (value: number) => formatCount(value),
            },
            {
              title: "日额度",
              dataIndex: "daily_review_budget",
              width: 110,
              render: (value: number) => `${formatCount(value)} 次`,
            },
          ]}
        />
      }
    />
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="管理后台" />
      <NikeConnectionNotice action="同步门店清单" />
      {/* 管理后台使用与所有人一致的个人 Nike 连接，没有共享账号。 */}
      <NikeCredentialCard />

      <SectionCard
        title="服务健康"
        extra={
          <Space size={8}>
            {offline ? <Tag color="orange">{OPS.offlineTag}</Tag> : null}
            <Button
              type="primary"
              loading={sync.isPending}
              disabled={!bound}
              onClick={confirmSync}
            >
              {BTN.syncStores}
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {sync.isError ? (
            <Alert type="error" showIcon message={ERR.opsSyncFailed} />
          ) : null}
          {sync.data?.ok ? (
            <Alert type="success" showIcon message={MSG.storesSynced(sync.data.synced ?? 0)} />
          ) : null}
          <Descriptions size="small" column={{ xs: 1, md: 3 }}>
            <Descriptions.Item label={OPS.healthRunningJobs}>
              {dashboard.data?.running_jobs ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label={OPS.healthFailedJobs}>
              {dashboard.data?.failed_jobs ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label={OPS.healthStoreCoverage}>
              {dashboard.data?.store_coverage ?? 0}
            </Descriptions.Item>
          </Descriptions>
          <Typography.Text type="secondary">
            {offline ? OPS.syncIntroOffline : OPS.syncIntro}
          </Typography.Text>
        </Space>
      </SectionCard>

      <SectionCard title="公司列表">{tenantCards}</SectionCard>
    </Space>
  );
}
