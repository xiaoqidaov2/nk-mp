import { Alert, Button, Card, Descriptions, Empty, Space, Switch, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { scanApi, taskApi } from "@/data/api";
import type { MonitorTask, ScanJob } from "@/data/types";

function formatCadence(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  return `${Math.round(seconds / 60)} 分钟`;
}

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const { canWrite } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const tasks = useQuery<MonitorTask[]>({ queryKey: ["tasks"], queryFn: () => taskApi.list() });
  const scans = useQuery<ScanJob[]>({
    queryKey: ["scans", taskId],
    queryFn: () => scanApi.list(taskId),
    enabled: Boolean(taskId),
  });

  const task = tasks.data?.find((item) => item.id === taskId);
  const related = scans.data ?? [];

  const toggle = useMutation({
    mutationFn: (isActive: boolean) => taskApi.update(taskId, { is_active: isActive }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] }),
  });
  const run = useMutation({
    mutationFn: () => scanApi.create({ task_id: taskId }),
    onSuccess: (job) => navigate(`/scans/${job.id}`),
  });

  if (tasks.isLoading) return <Card loading />;
  if (!task) return <Alert type="error" showIcon message="任务不存在或无权访问" />;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {run.isError ? <Alert type="error" showIcon message={(run.error as Error).message} /> : null}

      <Card
        size="small"
        title={task.name}
        extra={
          canWrite ? (
            <Space>
              <Switch
                checked={task.is_active}
                checkedChildren="启用"
                unCheckedChildren="暂停"
                loading={toggle.isPending}
                onChange={(checked) => toggle.mutate(checked)}
              />
              <Button
                type="primary"
                loading={run.isPending}
                disabled={task.store_count === 0 || task.style_count === 0}
                onClick={() => run.mutate()}
              >
                立即扫描
              </Button>
            </Space>
          ) : (
            <Typography.Text type="secondary">只读角色</Typography.Text>
          )
        }
      >
        <Descriptions size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="监控周期">{formatCadence(task.cadence_seconds)}</Descriptions.Item>
          <Descriptions.Item label="下次运行">
            {task.next_run_at ? new Date(task.next_run_at).toLocaleString("zh-CN") : "已暂停"}
          </Descriptions.Item>
          <Descriptions.Item label="上次运行">
            {task.last_run_at ? new Date(task.last_run_at).toLocaleString("zh-CN") : "尚未运行"}
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">
            {new Date(task.created_at).toLocaleString("zh-CN")}
          </Descriptions.Item>
        </Descriptions>
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <div>
            <Typography.Text type="secondary">提醒设置：</Typography.Text>{" "}
            {task.notify_on_restock ? <Tag>补货</Tag> : null}
            {task.notify_on_sold_out ? <Tag>售罄</Tag> : null}
            {task.notify_on_price_change ? <Tag>变价</Tag> : null}
            {!task.notify_on_restock && !task.notify_on_sold_out && !task.notify_on_price_change ? (
              <Tag>已关闭全部提醒</Tag>
            ) : null}
          </div>
          <div>
            <Typography.Text type="secondary">
              门店（{task.all_stores ? "全部" : task.store_count}）：
            </Typography.Text>
            {task.all_stores ? (
              <Tag color="blue">全部已同步门店（新门店自动纳入）</Tag>
            ) : null}
            {(task.store_codes ?? []).slice(0, 20).map((code) => (
              <Link key={code} to={`/stores/${code}`} style={{ marginRight: 8 }}>
                {code}
              </Link>
            ))}
            {task.store_count > 20 ? <span>等 {task.store_count} 家</span> : null}
          </div>
          <div>
            <Typography.Text type="secondary">货号（{task.style_count}）：</Typography.Text>
            {(task.style_codes ?? []).map((code) => (
              <Link key={code} to={`/styles/${code}`} style={{ marginRight: 8 }}>
                {code}
              </Link>
            ))}
          </div>
        </Space>
      </Card>

      <Card size="small" title="该任务的扫描记录">
        <Table<ScanJob>
          size="small"
          rowKey="id"
          loading={scans.isLoading}
          dataSource={related}
          locale={{ emptyText: <Empty description="该任务还没有扫描记录" /> }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={[
            {
              title: "扫描",
              dataIndex: "id",
              render: (value: string) => <Link to={`/scans/${value}`}>{value.slice(0, 8)}</Link>,
            },
            { title: "状态", dataIndex: "status", width: 110 },
            { title: "成功", dataIndex: "done_stores", width: 80 },
            { title: "失败", dataIndex: "failed_stores", width: 80 },
            { title: "数据不完整", dataIndex: "unknown_stores", width: 110 },
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
