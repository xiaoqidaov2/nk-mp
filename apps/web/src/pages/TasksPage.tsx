import { Alert, Button, Card, Empty, Space, Switch, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { scanApi, taskApi } from "@/data/api";
import type { MonitorTask, ScanJob } from "@/data/types";
import { useAuth } from "@/auth/AuthContext";

function formatCadence(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  return `${Math.round(seconds / 60)} 分钟`;
}

export function TasksPage() {
  const { canWrite } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const tasks = useQuery<MonitorTask[]>({ queryKey: ["tasks"], queryFn: () => taskApi.list() });
  const scans = useQuery<ScanJob[]>({ queryKey: ["scans"], queryFn: () => scanApi.list() });

  const toggle = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      taskApi.update(id, { is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] }),
  });

  const run = useMutation({
    mutationFn: (taskId: string) => scanApi.create({ task_id: taskId }),
    onSuccess: (job) => navigate(`/scans/${job.id}`),
  });

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {run.isError ? <Alert type="error" showIcon message={(run.error as Error).message} /> : null}

      <Card
        size="small"
        title="监控任务"
        extra={
          canWrite ? (
            <Button type="primary" onClick={() => navigate("/tasks/new")}>
              新建监控任务
            </Button>
          ) : (
            <Typography.Text type="secondary">只读角色不能修改任务</Typography.Text>
          )
        }
      >
        <Table<MonitorTask>
          size="small"
          rowKey="id"
          loading={tasks.isLoading}
          dataSource={tasks.data ?? []}
          locale={{ emptyText: <Empty description="还没有监控任务" /> }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={[
            { title: "任务名称", dataIndex: "name" },
            { title: "门店数", dataIndex: "store_count", width: 90 },
            { title: "货号数", dataIndex: "style_count", width: 90 },
            {
              title: "周期",
              dataIndex: "cadence_seconds",
              width: 100,
              render: (value: number) => formatCadence(value),
            },
            {
              title: "下次运行",
              dataIndex: "next_run_at",
              width: 170,
              render: (value: string | null) =>
                value ? new Date(value).toLocaleString("zh-CN") : "已暂停",
            },
            {
              title: "状态",
              dataIndex: "is_active",
              width: 110,
              render: (value: boolean, row: MonitorTask) => (
                <Switch
                  size="small"
                  checked={value}
                  disabled={!canWrite}
                  loading={toggle.isPending && toggle.variables?.id === row.id}
                  onChange={(checked) => toggle.mutate({ id: row.id, is_active: checked })}
                  checkedChildren="启用"
                  unCheckedChildren="暂停"
                />
              ),
            },
            {
              title: "操作",
              width: 170,
              render: (_: unknown, row: MonitorTask) => (
                <Space size={8}>
                  <Button
                    size="small"
                    disabled={!canWrite || row.store_count === 0 || row.style_count === 0}
                    loading={run.isPending && run.variables === row.id}
                    onClick={() => run.mutate(row.id)}
                  >
                    立即扫描
                  </Button>
                  <Link to={`/tasks/${row.id}`}>详情</Link>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Card size="small" title="最近扫描">
        <Table<ScanJob>
          size="small"
          rowKey="id"
          loading={scans.isLoading}
          dataSource={scans.data ?? []}
          locale={{ emptyText: <Empty description="还没有扫描记录" /> }}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          columns={[
            {
              title: "任务",
              dataIndex: "id",
              render: (value: string) => <Link to={`/scans/${value}`}>{value.slice(0, 8)}</Link>,
            },
            { title: "类型", dataIndex: "kind", width: 90 },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              render: (value: string) => (
                <Tag color={value === "SUCCEEDED" ? "green" : value === "RUNNING" ? "blue" : "orange"}>
                  {value}
                </Tag>
              ),
            },
            { title: "成功门店", dataIndex: "done_stores", width: 100 },
            { title: "失败门店", dataIndex: "failed_stores", width: 100 },
            { title: "未探测门店", dataIndex: "unknown_stores", width: 110 },
            {
              title: "创建时间",
              dataIndex: "created_at",
              width: 170,
              render: (value: string) => new Date(value).toLocaleString("zh-CN"),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
