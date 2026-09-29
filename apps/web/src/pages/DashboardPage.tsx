import { Alert, Card, Col, Empty, Row, Skeleton, Space, Statistic, Table, Tag } from "antd";
import { Link } from "react-router-dom";

import { FreshnessBadge } from "@/components/FreshnessBadge";
import { changeApi, dashboardApi } from "@/data/api";
import type { ChangeEvent, Dashboard } from "@/data/types";
import { useQuery } from "@tanstack/react-query";

const EVENT_LABELS: Record<ChangeEvent["event_type"], string> = {
  RESTOCK: "补货",
  SOLD_OUT: "售罄",
  PRICE_CHANGE: "变价",
  NEW_ARRIVAL_AT_STORE: "到店上新",
  SCAN_FAILED: "扫描失败",
  STRUCTURE_CHANGED: "结构变化",
};

function credentialTag(status: string) {
  if (status === "ACTIVE") return <Tag color="green">采集凭证正常</Tag>;
  if (status === "EXPIRING") return <Tag color="orange">采集凭证即将到期</Tag>;
  return <Tag color="red">需要重新授权</Tag>;
}

export function DashboardPage() {
  const dashboard = useQuery<Dashboard>({
    queryKey: ["dashboard"],
    queryFn: () => dashboardApi.get(),
  });
  const changes = useQuery<ChangeEvent[]>({
    queryKey: ["changes", "recent"],
    queryFn: () => changeApi.list({ days: 2 }),
  });

  if (dashboard.isLoading) return <Skeleton active />;
  if (dashboard.isError) {
    return <Alert type="error" message="概览加载失败" description={(dashboard.error as Error).message} />;
  }
  const data = dashboard.data as Dashboard;
  const cards = [
    { title: "运行中的监控任务", value: data.active_tasks },
    { title: "已监控货号", value: data.monitored_styles },
    { title: "已有探测结果的门店", value: `${data.monitored_stores} / ${data.store_coverage}` },
    { title: "今日补货", value: data.restock_today },
    { title: "今日售罄", value: data.sold_out_today },
    { title: "今日变价", value: data.price_change_today },
    { title: "今日到店上新", value: data.new_arrival_today },
    { title: "异常任务", value: data.failed_jobs + data.running_jobs },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={12} wrap>
            {credentialTag(data.credential_status)}
            <FreshnessBadge checkedAt={data.last_checked_at} />
            <span style={{ color: "#4b5563" }}>
              今日审核请求 {data.review_requests_today} / {data.daily_review_budget}
            </span>
          </Space>
          {(data.failed_jobs > 0 || data.credential_status === "NEEDS_AUTHORIZATION") && (
            <Alert
              type={data.credential_status === "NEEDS_AUTHORIZATION" ? "error" : "warning"}
              showIcon
              message={
                data.credential_status === "NEEDS_AUTHORIZATION"
                  ? "采集凭证已失效，监控已暂停数据更新，请在服务方后台重新导入凭证。"
                  : `今日有 ${data.failed_jobs} 个扫描任务未完全成功，可在任务详情中仅重试失败门店。`
              }
            />
          )}
        </Space>
      </Card>

      <Row gutter={[12, 12]}>
        {cards.map((card) => (
          <Col key={card.title} xs={12} md={6}>
            <Card size="small">
              <Statistic title={card.title} value={card.value} />
            </Card>
          </Col>
        ))}
      </Row>

      <Card size="small" title="近两日变化" extra={<Link to="/changes">查看全部</Link>}>
        <Table<ChangeEvent>
          size="small"
          rowKey="id"
          loading={changes.isLoading}
          dataSource={changes.data ?? []}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty description="暂无变化记录" /> }}
          columns={[
            {
              title: "类型",
              dataIndex: "event_type",
              width: 96,
              render: (value: ChangeEvent["event_type"]) => EVENT_LABELS[value] ?? value,
            },
            { title: "门店", dataIndex: "store_name", width: 160 },
            {
              title: "货号",
              dataIndex: "style_code",
              render: (value: string) =>
                value ? <Link to={`/styles/${value}`}>{value}</Link> : "—",
            },
            { title: "尺码", dataIndex: "size_label", width: 90, render: (v: string) => v || "—" },
            {
              title: "实付价",
              width: 150,
              render: (_: unknown, row: ChangeEvent) =>
                row.before_total_price === row.after_total_price
                  ? row.after_total_price != null
                    ? `¥${row.after_total_price.toFixed(2)}`
                    : "—"
                  : `${row.before_total_price != null ? `¥${row.before_total_price.toFixed(2)}` : "—"} → ${
                      row.after_total_price != null ? `¥${row.after_total_price.toFixed(2)}` : "—"
                    }`,
            },
            {
              title: "探测时间",
              dataIndex: "checked_at",
              width: 170,
              render: (value: string) => new Date(value).toLocaleString("zh-CN"),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
