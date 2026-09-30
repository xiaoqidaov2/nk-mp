import { Alert, Button, Col, Row, Skeleton, Space, Steps, Table, Tooltip } from "antd";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { useState } from "react";

import { EmptyState } from "@/components/EmptyState";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { TimeText } from "@/components/TimeText";
import { STATUS } from "@/components/statusTokens";
import { isCredentialActive } from "@/components/NikeCredentialCard";
import { BTN, DASHBOARD, EMPTY, ERR, GUIDE, formatPriceChange, formatQuota, formatSizeLabel, formatStoreLabel } from "@/copy";
import { changeApi, dashboardApi } from "@/data/api";
import type { ChangeEvent, Dashboard, NikeCredentialStatus } from "@/data/types";
import { useIsMobile } from "@/hooks/useIsMobile";

function credentialValue(raw: string): NikeCredentialStatus {
  return (STATUS.credential[raw] ? raw : "NOT_CONNECTED") as NikeCredentialStatus;
}

export function DashboardPage() {
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const [guideDismissed, setGuideDismissed] = useState(
    () => localStorage.getItem(GUIDE.storageKey) === "1",
  );

  const dashboard = useQuery<Dashboard>({
    queryKey: ["dashboard"],
    queryFn: () => dashboardApi.get(),
  });
  const changes = useQuery<ChangeEvent[]>({
    queryKey: ["changes", "recent"],
    queryFn: () => changeApi.list({ days: 2 }),
  });

  if (dashboard.isLoading) return <Skeleton active />;
  if (dashboard.isError || !dashboard.data) {
    return (
      <Alert
        type="error"
        showIcon
        message={ERR.overviewFailed}
        description={ERR.overviewFailedHint}
        action={
          <Button size="small" onClick={() => void dashboard.refetch()}>
            {BTN.refresh}
          </Button>
        }
      />
    );
  }

  const data = dashboard.data;
  const connected = isCredentialActive(credentialValue(data.credential_status));
  const showGuide = !guideDismissed && (!connected || data.active_tasks === 0);

  if (showGuide) {
    return (
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <PageHeader title="概览" />
        <SectionCard
          title="开始使用"
          extra={
            <Button
              type="link"
              size="small"
              style={{ padding: 0 }}
              onClick={() => {
                localStorage.setItem(GUIDE.storageKey, "1");
                setGuideDismissed(true);
              }}
            >
              {GUIDE.dismiss}
            </Button>
          }
        >
          <Steps
            size="small"
            direction={isMobile ? "vertical" : "horizontal"}
            items={GUIDE.steps.map((step, index) => ({
              title: step.title,
              description: (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 8,
                    alignItems: "flex-start",
                  }}
                >
                  <span style={{ color: "var(--color-text-secondary)" }}>{step.description}</span>
                  {index === 0 ? (
                    <Button className="touch-target" onClick={() => navigate("/account")}>
                      {step.action}
                    </Button>
                  ) : null}
                  {index === 1 ? (
                    connected ? (
                      <Button className="touch-target" onClick={() => navigate("/tasks/new")}>
                        {step.action}
                      </Button>
                    ) : (
                      <Tooltip title={step.disabledTooltip}>
                        <span style={{ display: "inline-block" }}>
                          <Button className="touch-target" disabled style={{ pointerEvents: "none" }}>
                            {step.action}
                          </Button>
                        </span>
                      </Tooltip>
                    )
                  ) : null}
                  {index === 2 ? (
                    <Button
                      className="touch-target"
                      disabled={(changes.data ?? []).length === 0}
                      onClick={() => navigate("/changes")}
                    >
                      {step.action}
                    </Button>
                  ) : null}
                </div>
              ),
            }))}
          />
          <div style={{ marginTop: 16 }}>
            <Link to="/account">{DASHBOARD.capabilityLink}</Link>
          </div>
        </SectionCard>
      </Space>
    );
  }

  const stats = [
    { label: DASHBOARD.statActiveTasks, value: data.active_tasks, to: "/tasks" },
    { label: DASHBOARD.statMonitoredStyles, value: data.monitored_styles, to: "/tasks" },
    {
      label: DASHBOARD.statMonitoredStores,
      value: `${data.monitored_stores} / ${data.store_coverage}`,
      to: "/tasks",
    },
    { label: DASHBOARD.statRestock, value: data.restock_today, to: "/changes?event_type=RESTOCK" },
    { label: DASHBOARD.statSoldOut, value: data.sold_out_today, to: "/changes?event_type=SOLD_OUT" },
    {
      label: DASHBOARD.statPriceChange,
      value: data.price_change_today,
      to: "/changes?event_type=PRICE_CHANGE",
    },
    {
      label: DASHBOARD.statNewArrival,
      value: data.new_arrival_today,
      to: "/changes?event_type=NEW_ARRIVAL_AT_STORE",
    },
    { label: DASHBOARD.statRunningJobs, value: data.running_jobs, to: "/tasks" },
  ];

  const recent = changes.data ?? [];
  const renderChangeCard = (row: ChangeEvent) => (
    <>
      <div className="mobile-card-list__row">
        <StatusPill domain="change" value={row.event_type} size="sm" />
        <Link to={`/stores/${row.store_code}`}>{formatStoreLabel(row.store_code, row.store_name)}</Link>
      </div>
      <CardRow label="货号">
        {row.style_code ? <Link to={`/styles/${row.style_code}`}>{row.style_code}</Link> : "—"}
      </CardRow>
      <CardRow label="实付价">{formatPriceChange(row.before_total_price, row.after_total_price)}</CardRow>
      <CardRow label="探测时间">
        <TimeText value={row.checked_at} />
      </CardRow>
    </>
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="概览" />

      <SectionCard>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={12} wrap>
            <StatusPill domain="credential" value={credentialValue(data.credential_status)} />
            <FreshnessBadge checkedAt={data.last_checked_at} />
            <span style={{ color: "var(--color-text-secondary)" }}>
              {formatQuota(data.review_requests_today, data.daily_review_budget)}
            </span>
          </Space>
          {!connected ? (
            <Alert
              type="error"
              showIcon
              message={
                data.credential_status === "NEEDS_AUTHORIZATION"
                  ? DASHBOARD.reauthRequired
                  : DASHBOARD.connectionNone
              }
              description={<Link to="/account">{DASHBOARD.connectionNoneAction}</Link>}
            />
          ) : data.failed_jobs > 0 ? (
            <Alert type="warning" showIcon message={DASHBOARD.failedToday(data.failed_jobs)} />
          ) : null}
        </Space>
      </SectionCard>

      <Row gutter={[12, 12]}>
        {stats.map((card) => (
          <Col key={card.label} xs={12} sm={12} md={6} xl={6}>
            <StatCard label={card.label} value={card.value} onClick={() => navigate(card.to)} />
          </Col>
        ))}
      </Row>

      <SectionCard title={DASHBOARD.recentChanges} extra={<Link to="/changes">{BTN.viewAll}</Link>}>
        <ResponsiveList
          items={recent}
          rowKey={(row) => row.id}
          loading={changes.isLoading}
          renderCard={renderChangeCard}
          empty={
            <EmptyState
              variant="no-data"
              description={EMPTY.overviewRecentChanges}
              compact
            />
          }
          table={
            <Table<ChangeEvent>
              size="small"
              rowKey="id"
              loading={changes.isLoading}
              dataSource={recent}
              pagination={{ pageSize: 8, hideOnSinglePage: true }}
              locale={{ emptyText: EMPTY.overviewRecentChanges }}
              columns={[
                {
                  title: "类型",
                  dataIndex: "event_type",
                  width: 100,
                  render: (value: ChangeEvent["event_type"]) => (
                    <StatusPill domain="change" value={value} size="sm" />
                  ),
                },
                {
                  title: "门店",
                  dataIndex: "store_name",
                  width: 180,
                  render: (value: string, row: ChangeEvent) => (
                    <Link to={`/stores/${row.store_code}`}>
                      {formatStoreLabel(row.store_code, value)}
                    </Link>
                  ),
                },
                {
                  title: "货号",
                  dataIndex: "style_code",
                  width: 140,
                  render: (value: string) =>
                    value ? <Link to={`/styles/${value}`}>{value}</Link> : "—",
                },
                {
                  title: "尺码",
                  dataIndex: "size_label",
                  width: 80,
                  render: (value: string) => formatSizeLabel(value),
                },
                {
                  title: "实付价",
                  width: 150,
                  render: (_: unknown, row: ChangeEvent) =>
                    formatPriceChange(row.before_total_price, row.after_total_price),
                },
                {
                  title: "探测时间",
                  dataIndex: "checked_at",
                  width: 150,
                  defaultSortOrder: "descend",
                  sorter: (a: ChangeEvent, b: ChangeEvent) => a.checked_at.localeCompare(b.checked_at),
                  render: (value: string) => <TimeText value={value} />,
                },
              ]}
            />
          }
        />
      </SectionCard>
    </Space>
  );
}
