import {
  Alert,
  App as AntdApp,
  Button,
  Descriptions,
  Progress,
  Segmented,
  Space,
  Table,
  Tooltip,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { EmptyState } from "@/components/EmptyState";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice, isCredentialActive, useNikeCredentialsQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatusPill } from "@/components/StatusPill";
import { errorCodeText } from "@/components/StatusTag";
import { BTN, EMPTY, ERR, MODALS, MSG, formatCount, formatDateTimeSeconds, formatScanId } from "@/copy";
import { scanApi, subscribeScanProgress } from "@/data/api";
import type { ScanJob, ScanStore } from "@/data/types";

type StreamAlert = "" | "interrupted" | "unavailable" | "unrecognized";

const STORE_STATUS_RANK: Record<ScanStore["status"], number> = {
  FAILED: 0,
  UNKNOWN: 1,
  RUNNING: 2,
  PENDING: 3,
  SUCCEEDED: 4,
};

export function ScanDetailPage() {
  const { jobId = "" } = useParams();
  const { canWrite } = useAuth();
  const queryClient = useQueryClient();
  const { message, modal } = AntdApp.useApp();
  const credentials = useNikeCredentialsQuery();
  const bound = credentials.data?.some((row) => isCredentialActive(row.status)) ?? false;
  const [live, setLive] = useState<ScanJob | null>(null);
  const [streamAlert, setStreamAlert] = useState<StreamAlert>("");
  const streamAlertRef = useRef<StreamAlert>("");
  const resumedRef = useRef(false);
  const [storeFilter, setStoreFilter] = useState("");

  const setAlert = (value: StreamAlert) => {
    streamAlertRef.current = value;
    setStreamAlert(value);
  };

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
        const current = streamAlertRef.current;
        if (current) {
          if (current !== "unrecognized" && !resumedRef.current) {
            resumedRef.current = true;
            message.success(MSG.progressResumed());
          }
          setAlert("");
        }
      },
      onDone: () => {
        void queryClient.invalidateQueries({ queryKey: ["scan", jobId] });
        void queryClient.invalidateQueries({ queryKey: ["changes"] });
        void stores.refetch();
      },
      onError: (errorMessage) => {
        setAlert(errorMessage.includes("解析") ? "unrecognized" : "unavailable");
      },
      onFallback: () => {
        resumedRef.current = false;
        setAlert("interrupted");
      },
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

  const filteredStores = useMemo(() => {
    const list = stores.data ?? [];
    if (storeFilter === "FAILED") return list.filter((row) => row.status === "FAILED");
    if (storeFilter === "UNKNOWN") return list.filter((row) => row.status === "UNKNOWN");
    return list;
  }, [stores.data, storeFilter]);

  if (job.isLoading) return <SectionCard loading>{null}</SectionCard>;
  if (job.isError || !current) {
    return <Alert type="error" showIcon message={ERR.scanNotFound} />;
  }

  const finished = !["QUEUED", "RUNNING"].includes(current.status);
  const pendingCount = current.failed_stores + current.unknown_stores;

  const reasonText = (code: string | null) => {
    if (!code) return "—";
    return (
      <Tooltip title={`原始代码：${code}`}>
        <span>{errorCodeText(code) ?? "系统原因"}</span>
      </Tooltip>
    );
  };

  const confirmRetry = () => {
    const count = pendingCount;
    const tpl = MODALS.retryStores(count);
    modal.confirm({
      title: tpl.title,
      content: tpl.content,
      okText: tpl.okText,
      cancelText: tpl.cancelText,
      onOk: () => {
        retry.mutate(undefined, {
          onSuccess: () => message.success(MSG.retriedStores(count)),
        });
      },
    });
  };

  const retryButton = (
    <Button
      disabled={!canWrite || !bound || !finished || pendingCount === 0}
      loading={retry.isPending}
      onClick={confirmRetry}
    >
      {BTN.retryFailedStores}
    </Button>
  );

  const storeCards = (
    <ResponsiveList
      items={filteredStores}
      rowKey={(row) => row.store_code}
      loading={stores.isLoading}
      empty={
        <EmptyState
          variant={current.status === "QUEUED" ? "not-probed" : "no-data"}
          description={
            current.status === "QUEUED" ? EMPTY.scanStoresWaiting : EMPTY.scanStoresNone
          }
          compact
        />
      }
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <Link to={`/stores/${row.store_code}`}>{row.store_code}</Link>
            <StatusPill domain="store" value={row.status} size="sm" />
          </div>
          <CardRow label="失败原因">{reasonText(row.error_code)}</CardRow>
          <CardRow label="产生变化">{formatCount(row.events_written)}</CardRow>
        </>
      )}
      table={
        <Table<ScanStore>
          size="small"
          rowKey="store_code"
          loading={stores.isLoading}
          dataSource={filteredStores}
          locale={{
            emptyText:
              current.status === "QUEUED" ? EMPTY.scanStoresWaiting : EMPTY.scanStoresNone,
          }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          columns={[
            {
              title: "门店号",
              dataIndex: "store_code",
              width: 90,
              defaultSortOrder: "ascend",
              sorter: (a: ScanStore, b: ScanStore) => a.store_code.localeCompare(b.store_code),
              render: (value: string) => <Link to={`/stores/${value}`}>{value}</Link>,
            },
            {
              title: "门店",
              dataIndex: "store_name",
              width: 160,
              sorter: (a: ScanStore, b: ScanStore) => a.store_name.localeCompare(b.store_name),
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 110,
              defaultSortOrder: "ascend",
              sorter: (a: ScanStore, b: ScanStore) =>
                STORE_STATUS_RANK[a.status] - STORE_STATUS_RANK[b.status],
              render: (value: ScanStore["status"]) => (
                <StatusPill domain="store" value={value} size="sm" />
              ),
            },
            {
              title: "失败原因",
              dataIndex: "error_code",
              width: 240,
              render: (value: string | null) => reasonText(value),
            },
            {
              title: "说明",
              dataIndex: "message",
              render: (value: string) =>
                value ? (
                  <Typography.Text type="secondary" ellipsis={{ tooltip: value }}>
                    {value}
                  </Typography.Text>
                ) : (
                  "—"
                ),
            },
            {
              title: "写入结果",
              dataIndex: "observations_written",
              width: 90,
              render: (value: number) => formatCount(value),
            },
            {
              title: "产生变化",
              dataIndex: "events_written",
              width: 90,
              render: (value: number) => formatCount(value),
            },
          ]}
        />
      }
    />
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="检查进度" />
      <NikeConnectionNotice action="重试失败门店" />

      {streamAlert === "interrupted" ? (
        <Alert type="warning" showIcon message={ERR.streamInterrupted} />
      ) : null}
      {streamAlert === "unrecognized" ? (
        <Alert type="warning" showIcon message={ERR.streamUnrecognized} />
      ) : null}
      {streamAlert === "unavailable" ? (
        <Alert
          type="error"
          showIcon
          message={ERR.streamUnavailable}
          action={
            <Button size="small" onClick={() => window.location.reload()}>
              {BTN.refreshPage}
            </Button>
          }
        />
      ) : null}

      <SectionCard
        title={`检查进度 · ${formatScanId(current.id)}`}
        description="自动刷新进度"
        extra={
          <Space wrap>
            <StatusPill domain="job" value={current.status} />
            {retryButton}
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Progress
            percent={Math.round(current.progress * 100)}
            status={finished ? "normal" : "active"}
          />
          <Descriptions size="small" column={{ xs: 1, md: 4 }}>
            <Descriptions.Item label="门店总数">{current.total_stores}</Descriptions.Item>
            <Descriptions.Item label="已完成">{current.done_stores}</Descriptions.Item>
            <Descriptions.Item label="失败">{current.failed_stores}</Descriptions.Item>
            <Descriptions.Item label="结果不完整">{current.unknown_stores}</Descriptions.Item>
          </Descriptions>
          <Typography.Text type="secondary">
            启动时间：{current.started_at ? formatDateTimeSeconds(current.started_at) : "尚未启动"}
            {current.finished_at
              ? ` · 结束时间：${formatDateTimeSeconds(current.finished_at)}`
              : ""}
          </Typography.Text>
          {current.error_summary ? (
            <Alert type="warning" showIcon message={current.error_summary} />
          ) : null}
        </Space>
      </SectionCard>

      <SectionCard
        title="逐店结果"
        extra={
          <Segmented
            value={storeFilter}
            onChange={(value) => setStoreFilter(String(value))}
            options={[
              { label: "全部", value: "" },
              { label: "只看失败", value: "FAILED" },
              { label: "只看结果不完整", value: "UNKNOWN" },
            ]}
          />
        }
      >
        {storeCards}
      </SectionCard>
    </Space>
  );
}
