import { Alert, App as AntdApp, Button, Descriptions, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice, NikeCredentialCard, isCredentialActive, useNikeCredentialsQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import {
  BTN,
  EMPTY,
  ERR,
  MODALS,
  MSG,
  OPS,
  cooldownReasonText,
  formatClock,
  formatCount,
  formatDateTimeSeconds,
  formatDuration,
} from "@/copy";
import { dashboardApi, opsApi } from "@/data/api";
import { opApiList } from "@/data/opsTypes";
import type { TenantRow } from "@/data/opsTypes";
import type {
  SchedulerCoolingAccount,
  SchedulerHealthInstance,
  SchedulerHealthWorker,
  SchedulerOverdueTask,
} from "@/data/types";

export function OpsPage() {
  const queryClient = useQueryClient();
  const credentials = useNikeCredentialsQuery();
  const bound = (credentials.data ?? []).some((row) => isCredentialActive(row.status));
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
  const schedulerHealth = useQuery({
    queryKey: ["ops", "scheduler-health"],
    queryFn: () => opsApi.schedulerHealth(),
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

  const schedulerData = schedulerHealth.data;

  const diskLine = schedulerHealth.data
    ? OPS.diskUsage(
        formatCount(schedulerHealth.data.disk.used_gb),
        formatCount(schedulerHealth.data.disk.total_gb),
        schedulerHealth.data.disk.used_percent,
      )
    : "";

  const schedulerInstances = (
    <Table<SchedulerHealthInstance>
      size="small"
      rowKey="instance_id"
      pagination={false}
      dataSource={schedulerData?.scheduler ?? []}
      locale={{ emptyText: OPS.emptyScheduler }}
      columns={[
        {
          title: OPS.colInstance,
          dataIndex: "instance_id",
          render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
        },
        {
          title: OPS.colLastSeen,
          dataIndex: "last_seen_at",
          width: 180,
          render: (value: string | null) => formatDateTimeSeconds(value),
        },
        {
          title: OPS.colState,
          dataIndex: "alive",
          width: 140,
          render: (alive: boolean) =>
            alive ? (
              <Tag color="green">{OPS.schedulerAlive}</Tag>
            ) : (
              <Tag color="red">{OPS.schedulerStale}</Tag>
            ),
        },
      ]}
    />
  );

  const workerNodes = (
    <Table<SchedulerHealthWorker>
      size="small"
      rowKey="instance_id"
      pagination={false}
      dataSource={schedulerData?.workers ?? []}
      locale={{ emptyText: OPS.emptyWorkers }}
      columns={[
        {
          title: OPS.colInstance,
          dataIndex: "instance_id",
          render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
        },
        {
          title: OPS.colLastSeen,
          dataIndex: "last_seen_at",
          width: 180,
          render: (value: string | null) => formatDateTimeSeconds(value),
        },
        {
          title: OPS.colRunningJobs,
          dataIndex: "running_jobs",
          width: 120,
          render: (value: number) => formatCount(value),
        },
      ]}
    />
  );

  const overdueTasks = (
    <ResponsiveList<SchedulerOverdueTask>
      items={schedulerData?.overdue_tasks ?? []}
      rowKey={(row) => row.task_id}
      empty={<div className="empty-state__description">{OPS.emptyOverdue}</div>}
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <span>{row.name}</span>
          </div>
          <CardRow label={OPS.colNextRun}>{formatDateTimeSeconds(row.next_run_at)}</CardRow>
          <CardRow label={OPS.colOverdue}>{formatDuration(row.overdue_seconds)}</CardRow>
          <CardRow label={OPS.colLastFinished}>{formatDateTimeSeconds(row.last_finished_at)}</CardRow>
        </>
      )}
      table={
        <Table<SchedulerOverdueTask>
          size="small"
          rowKey="task_id"
          pagination={false}
          dataSource={schedulerData?.overdue_tasks ?? []}
          locale={{ emptyText: OPS.emptyOverdue }}
          columns={[
            { title: OPS.colTask, dataIndex: "name" },
            {
              title: OPS.colNextRun,
              dataIndex: "next_run_at",
              width: 180,
              render: (value: string) => formatDateTimeSeconds(value),
            },
            {
              title: OPS.colOverdue,
              dataIndex: "overdue_seconds",
              width: 110,
              render: (value: number) => formatDuration(value),
            },
            {
              title: OPS.colLastFinished,
              dataIndex: "last_finished_at",
              width: 180,
              render: (value: string | null) => formatDateTimeSeconds(value),
            },
          ]}
        />
      }
    />
  );

  const coolingAccounts = (
    <ResponsiveList<SchedulerCoolingAccount>
      items={schedulerData?.cooling_accounts ?? []}
      rowKey={(row) => `${row.owner_user_id}:${row.credential_id}`}
      empty={<div className="empty-state__description">{OPS.emptyCooling}</div>}
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <Typography.Text code>{row.credential_id.slice(0, 8)}</Typography.Text>
          </div>
          <CardRow label={OPS.colOwner}>
            <Typography.Text code>{row.owner_user_id.slice(0, 8)}</Typography.Text>
          </CardRow>
          <CardRow label={OPS.colCooldownReason}>{cooldownReasonText(row.reason)}</CardRow>
          <CardRow label={OPS.colCooldownUntil}>{formatClock(row.until)}</CardRow>
        </>
      )}
      table={
        <Table<SchedulerCoolingAccount>
          size="small"
          rowKey={(row) => `${row.owner_user_id}:${row.credential_id}`}
          pagination={false}
          dataSource={schedulerData?.cooling_accounts ?? []}
          locale={{ emptyText: OPS.emptyCooling }}
          columns={[
            {
              title: OPS.colAccount,
              dataIndex: "credential_id",
              width: 140,
              render: (value: string) => <Typography.Text code>{value.slice(0, 8)}</Typography.Text>,
            },
            {
              title: OPS.colOwner,
              dataIndex: "owner_user_id",
              width: 140,
              render: (value: string) => <Typography.Text code>{value.slice(0, 8)}</Typography.Text>,
            },
            {
              title: OPS.colCooldownReason,
              dataIndex: "reason",
              render: (value: string | null) => cooldownReasonText(value),
            },
            {
              title: OPS.colCooldownUntil,
              dataIndex: "until",
              width: 120,
              render: (value: string) => formatClock(value),
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

      <SectionCard title={OPS.schedulerCard} description={OPS.schedulerNote}>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          {schedulerHealth.isError ? (
            <Alert type="error" showIcon message={ERR.commonFailure} />
          ) : null}
          {diskLine ? (
            <Alert
              type={schedulerHealth.data?.disk.warning ? "warning" : "info"}
              showIcon
              message={diskLine}
            />
          ) : null}
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Typography.Text strong>{OPS.schedulerInstances}</Typography.Text>
            {schedulerInstances}
          </Space>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Typography.Text strong>{OPS.workerNodes}</Typography.Text>
            {workerNodes}
          </Space>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Typography.Text strong>{OPS.overdueTasks}</Typography.Text>
            {overdueTasks}
          </Space>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Typography.Text strong>{OPS.coolingAccounts}</Typography.Text>
            {coolingAccounts}
          </Space>
        </Space>
      </SectionCard>
    </Space>
  );
}
