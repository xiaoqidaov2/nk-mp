import { Alert, App as AntdApp, Button, Descriptions, Space, Switch, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { EmptyState } from "@/components/EmptyState";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice, isCredentialActive, useNikeCredentialQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { useHasEnabledChannel } from "@/components/PushChannelsCard";
import { SectionCard } from "@/components/SectionCard";
import { StatusPill } from "@/components/StatusPill";
import { TimeText } from "@/components/TimeText";
import {
  BTN,
  EMPTY,
  ERR,
  MODALS,
  MSG,
  PUSH,
  TASK_LIST,
  formatCadence,
  formatCount,
  formatDate,
  formatScanId,
} from "@/copy";
import { scanApi, taskApi } from "@/data/api";
import type { MonitorTask, ScanJob } from "@/data/types";

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const { canWrite } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const credential = useNikeCredentialQuery();
  const bound = isCredentialActive(credential.data?.status);
  const hasEnabledChannel = useHasEnabledChannel();

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
    onSuccess: (updated, isActive) => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (isActive) {
        message.success(
          updated.next_run_at
            ? MSG.taskEnabled(updated.name, formatDate(updated.next_run_at))
            : MSG.taskEnabled(updated.name, "待定"),
        );
      } else {
        message.success(MSG.taskPaused(updated.name));
      }
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const run = useMutation({
    mutationFn: () => scanApi.create({ task_id: taskId }),
    onSuccess: (job) => {
      message.success(MSG.checkStarted());
      navigate(`/scans/${job.id}`);
    },
    onError: () => message.error(ERR.checkStartFailed),
  });

  const remove = useMutation({
    mutationFn: () => taskApi.remove(taskId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      message.success(MSG.taskDeleted(task?.name ?? ""));
      navigate("/tasks");
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const confirmDelete = () => {
    const tpl = MODALS.deleteTask(task?.name ?? "");
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      okButtonProps: { danger: true },
      onOk: () => remove.mutate(),
    });
  };

  const confirmPause = (onOk: () => void) => {
    const tpl = MODALS.pauseTask();
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      onOk,
    });
  };

  if (tasks.isLoading) return <SectionCard loading>{null}</SectionCard>;
  if (!task) {
    return (
      <Alert
        type="error"
        showIcon
        message={ERR.taskNotFound}
        action={
          <Button size="small" onClick={() => navigate("/tasks")}>
            返回任务列表
          </Button>
        }
      />
    );
  }

  const checkDisabled = !bound || task.store_count === 0 || task.style_count === 0;

  const checkButton = (
    <Button
      type="primary"
      loading={run.isPending}
      disabled={checkDisabled}
      onClick={() => run.mutate()}
    >
      {BTN.checkNow}
    </Button>
  );

  const scanCards = (
    <ResponsiveList
      items={related}
      rowKey={(job) => job.id}
      loading={scans.isLoading}
      empty={
        <EmptyState variant="no-data" description={EMPTY.taskScans} compact action={checkButton} />
      }
      renderCard={(job) => (
        <>
          <div className="mobile-card-list__row">
            <Link to={`/scans/${job.id}`}>{formatScanId(job.id)}</Link>
            <StatusPill domain="job" value={job.status} size="sm" />
          </div>
          <CardRow label="已完成">{TASK_LIST.scanCountLabel(job.done_stores, job.total_stores)}</CardRow>
          <CardRow label="失败">
            <span style={job.failed_stores > 0 ? { color: "var(--color-danger)" } : undefined}>
              {formatCount(job.failed_stores)}
            </span>
          </CardRow>
          <CardRow label="结果不完整">
            <span style={job.unknown_stores > 0 ? { color: "var(--color-warning)" } : undefined}>
              {formatCount(job.unknown_stores)}
            </span>
          </CardRow>
        </>
      )}
      table={
        <Table<ScanJob>
          size="small"
          rowKey="id"
          loading={scans.isLoading}
          dataSource={related}
          locale={{ emptyText: EMPTY.taskScans }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={[
            {
              title: "检查",
              dataIndex: "id",
              width: 110,
              defaultSortOrder: "descend",
              sorter: (a: ScanJob, b: ScanJob) => a.created_at.localeCompare(b.created_at),
              render: (value: string) => <Link to={`/scans/${value}`}>{formatScanId(value)}</Link>,
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              render: (value: ScanJob["status"]) => <StatusPill domain="job" value={value} size="sm" />,
            },
            {
              title: "已完成",
              width: 100,
              render: (_: unknown, row: ScanJob) =>
                TASK_LIST.scanCountLabel(row.done_stores, row.total_stores),
            },
            {
              title: "失败",
              dataIndex: "failed_stores",
              width: 80,
              render: (value: number) => (
                <span style={value > 0 ? { color: "var(--color-danger)" } : undefined}>
                  {formatCount(value)}
                </span>
              ),
            },
            {
              title: "结果不完整",
              dataIndex: "unknown_stores",
              width: 110,
              render: (value: number) => (
                <span style={value > 0 ? { color: "var(--color-warning)" } : undefined}>
                  {formatCount(value)}
                </span>
              ),
            },
            {
              title: "开始时间",
              dataIndex: "created_at",
              width: 150,
              render: (value: string) => <TimeText value={value} absolute={false} />,
            },
          ]}
        />
      }
    />
  );

  const notifyTags = [
    task.notify_on_restock ? { key: "restock", label: "补货" } : null,
    task.notify_on_sold_out ? { key: "soldout", label: "售罄" } : null,
    task.notify_on_price_change ? { key: "price", label: "变价" } : null,
  ].filter((item): item is { key: string; label: string } => item !== null);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title={task.name} back={{ onClick: () => navigate("/tasks") }} />
      <NikeConnectionNotice action="启用任务或启动检查" />

      <SectionCard
        title={task.name}
        extra={
          canWrite ? (
            <Space wrap>
              <Switch
                checked={task.is_active}
                disabled={!bound}
                checkedChildren={BTN.enable}
                unCheckedChildren={BTN.pause}
                loading={toggle.isPending}
                onChange={(checked) => {
                  if (!checked) {
                    confirmPause(() => toggle.mutate(false));
                  } else {
                    toggle.mutate(true);
                  }
                }}
              />
              {checkButton}
              <Button danger onClick={confirmDelete}>
                {BTN.deleteTask}
              </Button>
            </Space>
          ) : (
            <Typography.Text type="secondary">{EMPTY.readonlyTasks}</Typography.Text>
          )
        }
      >
        <Descriptions size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="检查频率">{formatCadence(task.cadence_seconds)}</Descriptions.Item>
          <Descriptions.Item label="下次检查">
            {task.next_run_at ? formatDate(task.next_run_at) : BTN.pause}
          </Descriptions.Item>
          <Descriptions.Item label="上次检查">
            {task.last_run_at ? formatDate(task.last_run_at) : "还没检查过"}
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">{formatDate(task.created_at)}</Descriptions.Item>
        </Descriptions>
        <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
          <div>
            <Typography.Text type="secondary">提醒设置：</Typography.Text>{" "}
            {notifyTags.length === 0 ? (
              <Tag>已关闭全部提醒</Tag>
            ) : (
              notifyTags.map((item) => <Tag key={item.key}>{item.label}</Tag>)
            )}
          </div>
          {notifyTags.length > 0 && hasEnabledChannel === false ? (
            <Alert
              type="info"
              showIcon
              message={PUSH.noChannelHint}
              action={<Link to="/account">{PUSH.noChannelLink}</Link>}
            />
          ) : null}
          <div>
            <Typography.Text type="secondary">
              门店（{task.all_stores ? "全部" : task.store_count}）：
            </Typography.Text>
            {task.all_stores ? (
              <Tag color="blue">全部已登记门店（新门店自动加入）</Tag>
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
      </SectionCard>

      <SectionCard title="这个任务的检查记录">{scanCards}</SectionCard>
    </Space>
  );
}
