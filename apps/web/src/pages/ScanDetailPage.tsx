import { Alert, Button, Card, Descriptions, Empty, Progress, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { scanApi, subscribeScanProgress } from "@/data/api";
import type { ScanJob, ScanStore } from "@/data/types";
import { useAuth } from "@/auth/AuthContext";

const STORE_STATUS_COLOR: Record<ScanStore["status"], string> = {
  PENDING: "default",
  RUNNING: "blue",
  SUCCEEDED: "green",
  FAILED: "red",
  UNKNOWN: "orange",
};

const STORE_STATUS_LABEL: Record<ScanStore["status"], string> = {
  PENDING: "等待中",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  UNKNOWN: "数据不完整",
};

const ERROR_LABEL: Record<string, string> = {
  EMPTY_COMPLETED: "接口返回空结果（不可信）",
  POLL_TIMEOUT: "轮询超时",
  WAF_CHALLENGE: "被风控挑战拦截",
  AUTH_REQUIRED: "凭证失效",
  TRANSPORT_ERROR: "网络中断",
  BATCH_SUSPECT: "批次结果不完整",
  ITEM_QUANTITY_LIMIT: "数量超出单次上限",
  STRUCTURE_MISSING: "缺少尺码结构",
  UNEXPECTED_STATUS: "上游返回异常状态",
  BUDGET_EXHAUSTED: "配额已用尽",
};

export function ScanDetailPage() {
  const { jobId = "" } = useParams();
  const { canWrite } = useAuth();
  const queryClient = useQueryClient();
  const [live, setLive] = useState<ScanJob | null>(null);
  const [streamError, setStreamError] = useState("");

  const job = useQuery<ScanJob>({
    queryKey: ["scan", jobId],
    queryFn: () => scanApi.get(jobId),
    enabled: Boolean(jobId),
  });

  const stores = useQuery<ScanStore[]>({
    queryKey: ["scan", jobId, "stores"],
    queryFn: () => scanApi.stores(jobId),
    enabled: Boolean(jobId),
  });

  useEffect(() => {
    if (!jobId) return undefined;
    const unsubscribe = subscribeScanProgress(jobId, {
      onProgress: (updated) => {
        setLive(updated);
        setStreamError("");
      },
      onDone: () => {
        void queryClient.invalidateQueries({ queryKey: ["scan", jobId] });
        void queryClient.invalidateQueries({ queryKey: ["changes"] });
        void stores.refetch();
      },
      onError: (message) => setStreamError(message),
    });
    return unsubscribe;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  const current = live ?? job.data;
  const retry = useMutation({
    mutationFn: () => scanApi.retryFailed(jobId),
    onSuccess: (updated) => {
      setLive(updated);
      void stores.refetch();
    },
  });

  if (job.isLoading) return <Card loading />;
  if (job.isError || !current) {
    return <Alert type="error" showIcon message="扫描任务不存在或无权访问" />;
  }

  const finished = !["QUEUED", "RUNNING"].includes(current.status);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {streamError ? (
        <Alert
          type="warning"
          showIcon
          message="实时进度连接已断开，正在回退为轮询"
          description={streamError}
        />
      ) : null}

      <Card
        size="small"
        title={`扫描进度 · ${current.id.slice(0, 8)}`}
        extra={
          <Space>
            <Tag color={finished ? "default" : "processing"}>
              {finished ? current.status : "运行中"}
            </Tag>
            <Button
              disabled={!canWrite || !finished || current.failed_stores + current.unknown_stores === 0}
              loading={retry.isPending}
              onClick={() => retry.mutate()}
            >
              仅重试失败门店
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Progress percent={Math.round(current.progress * 100)} status={finished ? "normal" : "active"} />
          <Descriptions size="small" column={{ xs: 1, md: 4 }}>
            <Descriptions.Item label="门店总数">{current.total_stores}</Descriptions.Item>
            <Descriptions.Item label="已完成">{current.done_stores}</Descriptions.Item>
            <Descriptions.Item label="失败">{current.failed_stores}</Descriptions.Item>
            <Descriptions.Item label="数据不完整">{current.unknown_stores}</Descriptions.Item>
          </Descriptions>
          <Typography.Text type="secondary">
            启动时间：
            {current.started_at ? new Date(current.started_at).toLocaleString("zh-CN") : "尚未启动"}
            {current.finished_at
              ? ` · 结束时间：${new Date(current.finished_at).toLocaleString("zh-CN")}`
              : ""}
          </Typography.Text>
          {current.error_summary ? (
            <Alert type="warning" showIcon message={current.error_summary} />
          ) : null}
        </Space>
      </Card>

      <Card size="small" title="逐店结果">
        <Table<ScanStore>
          size="small"
          rowKey="store_code"
          loading={stores.isLoading}
          dataSource={stores.data ?? []}
          locale={{ emptyText: <Empty description="暂无门店结果" /> }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          columns={[
            { title: "门店号", dataIndex: "store_code", width: 90 },
            { title: "门店", dataIndex: "store_name", width: 180 },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              render: (value: ScanStore["status"]) => (
                <Tag color={STORE_STATUS_COLOR[value]}>{STORE_STATUS_LABEL[value]}</Tag>
              ),
            },
            {
              title: "失败原因",
              dataIndex: "error_code",
              width: 200,
              render: (value: string | null) => (value ? ERROR_LABEL[value] ?? value : "—"),
            },
            { title: "说明", dataIndex: "message" },
            { title: "写入观测", dataIndex: "observations_written", width: 100 },
            { title: "产生变化", dataIndex: "events_written", width: 100 },
          ]}
        />
      </Card>
    </Space>
  );
}
