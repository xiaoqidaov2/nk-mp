import { Alert, Button, Card, Descriptions, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { opApiList } from "@/data/opsTypes";
import { dashboardApi, opsApi } from "@/data/api";
import type { CredentialStatus } from "@/data/types";

export function OpsPage() {
  const queryClient = useQueryClient();
  const credential = useQuery<CredentialStatus>({
    queryKey: ["credential"],
    queryFn: () => opsApi.credential(),
    retry: false,
  });
  const tenants = useQuery({
    queryKey: ["ops", "tenants"],
    queryFn: () => opApiList(),
  });
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.get() });

  const refresh = useMutation({
    mutationFn: () => opsApi.refreshCredential(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["credential"] }),
  });
  const sync = useMutation({
    mutationFn: () => opsApi.syncStores(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["stores"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {credential.data?.status === "NEEDS_AUTHORIZATION" ? (
        <Alert
          type="error"
          showIcon
          message="采集凭证需要重新授权"
          description="请在服务端执行 nike-mp import-credential 导入新的 token 文件；系统不会自动调用任何客户端调试流程。"
        />
      ) : null}

      <Card
        size="small"
        title="采集凭证"
        extra={
          <Space>
            <Button size="small" loading={refresh.isPending} onClick={() => refresh.mutate()}>
              立即续期
            </Button>
            <Button
              size="small"
              type="primary"
              loading={sync.isPending}
              onClick={() => sync.mutate()}
            >
              同步门店清单
            </Button>
          </Space>
        }
      >
        {refresh.isError ? (
          <Alert type="warning" showIcon message={(refresh.error as Error).message} />
        ) : null}
        <Descriptions size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="状态">
            <Tag color={credential.data?.status === "ACTIVE" ? "green" : "orange"}>
              {credential.data?.status ?? "未知"}
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
      </Card>

      <Card size="small" title="服务健康">
        {sync.isError ? (
          <Alert type="error" showIcon message={(sync.error as Error).message} style={{ marginBottom: 12 }} />
        ) : null}
        {sync.data?.ok ? (
          <Alert
            type="success"
            showIcon
            message={`门店清单已同步，共 ${sync.data.synced ?? 0} 家`}
            style={{ marginBottom: 12 }}
          />
        ) : null}
        <Descriptions size="small" column={{ xs: 1, md: 3 }}>
          <Descriptions.Item label="运行中任务">{dashboard.data?.running_jobs ?? 0}</Descriptions.Item>
          <Descriptions.Item label="今日异常任务">{dashboard.data?.failed_jobs ?? 0}</Descriptions.Item>
          <Descriptions.Item label="已同步门店">{dashboard.data?.store_coverage ?? 0}</Descriptions.Item>
        </Descriptions>
        <Typography.Text type="secondary">
          账号池、调度参数与审计日志属于后续迭代范围，当前版本以单账号与配额硬上限运行。
        </Typography.Text>
      </Card>

      <Card size="small" title="租户列表">
        <Table
          size="small"
          rowKey="id"
          loading={tenants.isLoading}
          dataSource={tenants.data ?? []}
          pagination={false}
          columns={[
            { title: "名称", dataIndex: "name" },
            { title: "标识", dataIndex: "slug", width: 140 },
            {
              title: "类型",
              dataIndex: "is_platform",
              width: 110,
              render: (value: boolean) => (value ? <Tag color="blue">服务方</Tag> : <Tag>客户</Tag>),
            },
            { title: "任务数", dataIndex: "task_count", width: 90 },
            { title: "日配额", dataIndex: "daily_review_budget", width: 110 },
          ]}
        />
      </Card>
    </Space>
  );
}
