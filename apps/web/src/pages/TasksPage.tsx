import { Alert, App as AntdApp, Button, Space, Table, Tag, Tooltip, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice, isCredentialActive, useNikeCredentialQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatusPill } from "@/components/StatusPill";
import {
  EditCadenceModal,
  EditSizeScopeModal,
  EditStoreScopeModal,
} from "@/components/TaskScopeModals";
import { TimeText } from "@/components/TimeText";
import { useAuth } from "@/auth/AuthContext";
import {
  BTN,
  COOLDOWN,
  EMPTY,
  ERR,
  MODALS,
  MSG,
  TASK_CADENCE,
  TASK_LIST,
  TASK_SCOPE,
  formatCadence,
  formatClock,
  formatDate,
  formatCount,
  formatScanId,
  formatStoreScope,
} from "@/copy";
import { scanApi, taskApi } from "@/data/api";
import type { MonitorTask, ScanJob } from "@/data/types";

export function TasksPage() {
  const { canWrite } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const credential = useNikeCredentialQuery();
  const bound = isCredentialActive(credential.data?.status);
  const [scope, setScope] = useState<{ task: MonitorTask; kind: "sizes" | "stores" | "cadence" } | null>(null);

  const tasks = useQuery<MonitorTask[]>({ queryKey: ["tasks"], queryFn: () => taskApi.list() });
  const scans = useQuery<ScanJob[]>({ queryKey: ["scans"], queryFn: () => scanApi.list() });

  const taskName = (taskId: string | null) =>
    tasks.data?.find((item) => item.id === taskId)?.name ?? TASK_LIST.manualCheck;

  // 合并检查作业没有单个 task_id：显示成"手动检查"会误导（那是系统按周期自动合并的作业）。
  const relatedTask = (job: ScanJob) => {
    if (job.task_id) {
      return taskName(job.task_id);
    }
    return job.task_ids.length > 1
      ? TASK_LIST.mergedCheck(job.task_ids.length)
      : TASK_LIST.manualCheck;
  };

  const toggle = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      taskApi.update(id, { is_active }),
    onSuccess: (updated, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (variables.is_active) {
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
    mutationFn: (taskId: string) => scanApi.create({ task_id: taskId }),
    onSuccess: (job) => {
      message.success(MSG.checkStarted());
      navigate(`/scans/${job.id}`);
    },
    onError: () => message.error(ERR.checkStartFailed),
  });

  const remove = useMutation({
    mutationFn: (taskId: string) => taskApi.remove(taskId),
    onSuccess: (_result, taskId) => {
      const name = tasks.data?.find((item) => item.id === taskId)?.name ?? "";
      message.success(MSG.taskDeleted(name));
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const confirmDelete = (task: MonitorTask) => {
    const tpl = MODALS.deleteTask(task.name);
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      okButtonProps: { danger: true },
      onOk: () => remove.mutate(task.id),
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

  const cooldownBadge = (task: MonitorTask) =>
    task.cooldown_until ? (
      <Tooltip title={COOLDOWN.note}>
        <Tag color="orange" style={{ marginInlineEnd: 0 }}>
          {COOLDOWN.tag} · {COOLDOWN.hint(formatClock(task.cooldown_until))}
        </Tag>
      </Tooltip>
    ) : null;

  const checkDisabledReason = (task: MonitorTask): string | null => {
    if (!canWrite) return TASK_LIST.disabledReadonly;
    if (!bound) return TASK_LIST.disabledNotConnected;
    if (task.style_count === 0) return TASK_LIST.disabledNoStyles;
    if (!task.all_stores && task.store_count === 0) return TASK_LIST.disabledNoStores;
    return null;
  };

  const checkButton = (task: MonitorTask, block = false) => {
    const reason = checkDisabledReason(task);
    const button = (
      <Button
        block={block}
        size="small"
        disabled={reason !== null}
        loading={run.isPending && run.variables === task.id}
        onClick={() => run.mutate(task.id)}
      >
        {BTN.checkNow}
      </Button>
    );
    if (!reason) return button;
    // 禁用按钮在浏览器中不派发鼠标事件，包一层 span 让 Tooltip 能出现。
    return (
      <Tooltip title={reason}>
        <span style={{ display: "inline-block", width: block ? "100%" : undefined }}>{button}</span>
      </Tooltip>
    );
  };

  const newTaskButton = (
    <Tooltip title={!bound ? TASK_LIST.connectFirstTooltip : undefined}>
      <span
        style={{ display: "inline-block", cursor: !bound ? "pointer" : undefined }}
        onClick={() => {
          if (!bound) navigate("/account");
        }}
      >
        <Button
          type="primary"
          disabled={!bound}
          style={!bound ? { pointerEvents: "none" } : undefined}
          onClick={() => navigate("/tasks/new")}
        >
          {BTN.newTask}
        </Button>
      </span>
    </Tooltip>
  );

  const taskCards = (
    <ResponsiveList
      items={tasks.data ?? []}
      rowKey={(task) => task.id}
      loading={tasks.isLoading}
      empty={
        <EmptyState
          variant="no-data"
          description={canWrite ? EMPTY.taskList : EMPTY.readonlyTasks}
          compact
        />
      }
      renderCard={(task) => (
        <>
          <div className="mobile-card-list__row">
            <Link to={`/tasks/${task.id}`}>{task.name}</Link>
            <Tag color={task.is_active ? "green" : undefined}>
              {task.is_active ? TASK_LIST.statusActive : TASK_LIST.statusPaused}
            </Tag>
          </div>
          <CardRow label="检查范围">
            <Space size={4} wrap>
              <span>{formatStoreScope(task.all_stores, task.store_count)}</span>
              {task.size_labels?.length ? <Tag>{TASK_SCOPE.pickedSizes(task.size_labels.length)}</Tag> : null}
            </Space>
          </CardRow>
          <CardRow label="货号数">{`${formatCount(task.style_count)} 个`}</CardRow>
          <CardRow label="检查频率">{formatCadence(task.cadence_seconds)}</CardRow>
          {task.cooldown_until ? (
            <CardRow label="检查状态">{cooldownBadge(task)}</CardRow>
          ) : null}
          <div className="mobile-card-list__actions">
            {checkButton(task, true)}
            <Link to={`/tasks/${task.id}`}>{BTN.view}</Link>
            {canWrite ? (
              <>
                <Button size="small" onClick={() => setScope({ task, kind: "sizes" })}>
                  {TASK_SCOPE.sizeAction}
                </Button>
                <Button size="small" onClick={() => setScope({ task, kind: "stores" })}>
                  {TASK_SCOPE.storeAction}
                </Button>
                <Button size="small" onClick={() => setScope({ task, kind: "cadence" })}>
                  {TASK_CADENCE.action}
                </Button>
                <Button
                  size="small"
                  loading={toggle.isPending && toggle.variables?.id === task.id}
                  onClick={() => {
                    if (task.is_active) {
                      confirmPause(() => toggle.mutate({ id: task.id, is_active: false }));
                    } else {
                      toggle.mutate({ id: task.id, is_active: true });
                    }
                  }}
                >
                  {task.is_active ? BTN.pause : BTN.enable}
                </Button>
                <Button danger size="small" onClick={() => confirmDelete(task)}>
                  {BTN.deleteTask}
                </Button>
              </>
            ) : null}
          </div>
        </>
      )}
      table={
        <Table<MonitorTask>
          size="small"
          rowKey="id"
          loading={tasks.isLoading}
          dataSource={tasks.data ?? []}
          locale={{
            emptyText: canWrite ? EMPTY.taskList : EMPTY.readonlyTasks,
          }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={[
            {
              title: "任务名称",
              dataIndex: "name",
              sorter: (a: MonitorTask, b: MonitorTask) => a.name.localeCompare(b.name),
              defaultSortOrder: "ascend",
              render: (value: string, row: MonitorTask) => <Link to={`/tasks/${row.id}`}>{value}</Link>,
            },
            {
              title: "检查范围",
              dataIndex: "store_count",
              width: 150,
              render: (_: unknown, row: MonitorTask) => (
                <Space size={4} wrap>
                  <span>{formatStoreScope(row.all_stores, row.store_count)}</span>
                  {row.size_labels?.length ? (
                    <Tag>{TASK_SCOPE.pickedSizes(row.size_labels.length)}</Tag>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "货号数",
              dataIndex: "style_count",
              width: 90,
              sorter: (a: MonitorTask, b: MonitorTask) => a.style_count - b.style_count,
              render: (value: number) => `${formatCount(value)} 个`,
            },
            {
              title: "检查频率",
              dataIndex: "cadence_seconds",
              width: 100,
              sorter: (a: MonitorTask, b: MonitorTask) => a.cadence_seconds - b.cadence_seconds,
              render: (value: number) => formatCadence(value),
            },
            {
              title: "下次检查",
              dataIndex: "next_run_at",
              width: 150,
              sorter: (a: MonitorTask, b: MonitorTask) =>
                (a.next_run_at ?? "").localeCompare(b.next_run_at ?? ""),
              defaultSortOrder: "ascend",
              render: (value: string | null) => (value ? formatDate(value) : BTN.pause),
            },
            {
              title: "状态",
              dataIndex: "is_active",
              width: 220,
              sorter: (a: MonitorTask, b: MonitorTask) => Number(a.is_active) - Number(b.is_active),
              render: (value: boolean, row: MonitorTask) => (
                <Space size={4} wrap>
                  <Tag color={value ? "green" : undefined}>
                    {value ? TASK_LIST.statusActive : TASK_LIST.statusPaused}
                  </Tag>
                  {cooldownBadge(row)}
                </Space>
              ),
            },
            {
              title: "操作",
              width: 360,
              render: (_: unknown, row: MonitorTask) => (
                <Space size={8} wrap>
                  {checkButton(row)}
                  <Link to={`/tasks/${row.id}`}>{BTN.view}</Link>
                  {canWrite ? (
                    <>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => setScope({ task: row, kind: "sizes" })}
                      >
                        {TASK_SCOPE.sizeAction}
                      </Button>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => setScope({ task: row, kind: "stores" })}
                      >
                        {TASK_SCOPE.storeAction}
                      </Button>
                      <Button
                        type="link"
                        size="small"
                        onClick={() => setScope({ task: row, kind: "cadence" })}
                      >
                        {TASK_CADENCE.action}
                      </Button>
                      <Button
                        type="link"
                        size="small"
                        loading={toggle.isPending && toggle.variables?.id === row.id}
                        onClick={() => {
                          if (row.is_active) {
                            confirmPause(() => toggle.mutate({ id: row.id, is_active: false }));
                          } else {
                            toggle.mutate({ id: row.id, is_active: true });
                          }
                        }}
                      >
                        {row.is_active ? BTN.pause : BTN.enable}
                      </Button>
                      <Button type="link" size="small" danger onClick={() => confirmDelete(row)}>
                        {BTN.deleteTask}
                      </Button>
                    </>
                  ) : null}
                </Space>
              ),
            },
          ]}
        />
      }
    />
  );

  const scanCards = (
    <ResponsiveList
      items={scans.data ?? []}
      rowKey={(job) => job.id}
      loading={scans.isLoading}
      empty={<EmptyState variant="no-data" description={EMPTY.scanList} compact />}
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
          dataSource={scans.data ?? []}
          locale={{ emptyText: EMPTY.scanList }}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
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
              title: "关联任务",
              dataIndex: "task_id",
              width: 150,
              render: (_value: string | null, record: ScanJob) => relatedTask(record),
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              sorter: (a: ScanJob, b: ScanJob) => a.status.localeCompare(b.status),
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
              sorter: (a: ScanJob, b: ScanJob) => a.failed_stores - b.failed_stores,
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
              sorter: (a: ScanJob, b: ScanJob) => a.unknown_stores - b.unknown_stores,
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
              defaultSortOrder: "descend",
              sorter: (a: ScanJob, b: ScanJob) => a.created_at.localeCompare(b.created_at),
              render: (value: string) => <TimeText value={value} absolute={false} />,
            },
          ]}
        />
      }
    />
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="监控任务" />
      <NikeConnectionNotice action="启动检查或启用任务" />

      {tasks.isError || scans.isError ? (
        <Alert
          type="error"
          showIcon
          message={ERR.taskListFailed}
          action={
            <Button
              size="small"
              onClick={() => {
                void tasks.refetch();
                void scans.refetch();
              }}
            >
              {BTN.refresh}
            </Button>
          }
        />
      ) : null}

      <SectionCard
        title="监控任务"
        extra={
          canWrite ? (
            newTaskButton
          ) : (
            <Typography.Text type="secondary">{EMPTY.readonlyTasks}</Typography.Text>
          )
        }
      >
        {taskCards}
      </SectionCard>

      <SectionCard title="最近检查">{scanCards}</SectionCard>

      <EditSizeScopeModal
        task={scope?.kind === "sizes" ? scope.task : null}
        open={scope?.kind === "sizes"}
        onClose={() => setScope(null)}
      />
      <EditStoreScopeModal
        task={scope?.kind === "stores" ? scope.task : null}
        open={scope?.kind === "stores"}
        onClose={() => setScope(null)}
      />
      <EditCadenceModal
        task={scope?.kind === "cadence" ? scope.task : null}
        open={scope?.kind === "cadence"}
        onClose={() => setScope(null)}
      />
    </Space>
  );
}
