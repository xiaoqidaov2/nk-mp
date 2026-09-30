import { Alert, Button, Pagination, Segmented, Space, Table, Tooltip, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatusPill } from "@/components/StatusPill";
import { availabilityLabel } from "@/components/StatusTag";
import { STATUS } from "@/components/statusTokens";
import {
  BTN,
  CHANGES,
  EMPTY,
  ERR,
  formatDate,
  formatPromotions,
  formatPriceChange,
  formatRelative,
  formatSizeLabel,
  formatStoreLabel,
} from "@/copy";
import { changeApi } from "@/data/api";
import { useIsMobile } from "@/hooks/useIsMobile";
import type { Availability, ChangeEvent } from "@/data/types";

const FILTER_ORDER: ChangeEvent["event_type"][] = [
  "RESTOCK",
  "SOLD_OUT",
  "PRICE_CHANGE",
  "NEW_ARRIVAL_AT_STORE",
  "SCAN_FAILED",
  "STRUCTURE_CHANGED",
];

function stateLabel(value: string | null): string {
  if (!value || !(value in STATUS.availability)) return "—";
  return availabilityLabel(value as Availability);
}

export function ChangesPage() {
  const isMobile = useIsMobile();
  const [searchParams] = useSearchParams();
  const [filter, setFilter] = useState(searchParams.get("event_type") ?? "");
  const [cardPage, setCardPage] = useState(1);
  const changes = useQuery<ChangeEvent[]>({
    queryKey: ["changes", filter],
    queryFn: () => changeApi.list({ event_type: filter, days: 30 }),
  });

  const filterLabel =
    filter && FILTER_ORDER.includes(filter as ChangeEvent["event_type"])
      ? STATUS.change[filter].label
      : "";

  const segmentedOptions = [
    { label: CHANGES.filterAll, value: "" },
    ...FILTER_ORDER.map((type) => ({ label: STATUS.change[type].label, value: type })),
  ];

  const items = changes.data ?? [];
  const pageSize = 20;
  const pagedItems = items.slice((cardPage - 1) * pageSize, cardPage * pageSize);

  const renderCard = (row: ChangeEvent) => (
    <>
      <div className="mobile-card-list__row">
        <StatusPill domain="change" value={row.event_type} size="sm" />
        <Link to={`/stores/${row.store_code}`}>{formatStoreLabel(row.store_code, row.store_name)}</Link>
      </div>
      <CardRow label="货号">
        {row.style_code ? (
          <Link to={`/styles/${row.style_code}?store=${row.store_code}&size=${row.size_label}`}>
            {row.style_code}
          </Link>
        ) : (
          "—"
        )}
      </CardRow>
      <CardRow label="尺码">{formatSizeLabel(row.size_label)}</CardRow>
      <CardRow label="有货状态变化">
        {row.before_status || row.after_status
          ? `${stateLabel(row.before_status)} → ${stateLabel(row.after_status)}`
          : "—"}
      </CardRow>
      <CardRow label="实付价变化">
        {formatPriceChange(row.before_total_price, row.after_total_price)}
      </CardRow>
      <CardRow label="探测时间">{formatDate(row.checked_at)}</CardRow>
    </>
  );

  const emptyState =
    filter && !changes.isLoading ? (
      <EmptyState
        variant="no-result"
        description={EMPTY.changesFiltered(filterLabel)}
        action={<Button onClick={() => setFilter("")}>{BTN.viewAll}</Button>}
        compact
      />
    ) : (
      <EmptyState variant="no-data" description={EMPTY.changesNone} compact />
    );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="变化流水" />

      <SectionCard>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Segmented
            options={segmentedOptions}
            value={filter}
            onChange={(value) => {
              setFilter(String(value));
              setCardPage(1);
            }}
          />
          <Typography.Text type="secondary" style={{ fontSize: "var(--fs-caption)" }}>
            {CHANGES.filterNote}
          </Typography.Text>
        </Space>
      </SectionCard>

      {changes.isError ? (
        <Alert
          type="error"
          showIcon
          message={ERR.changesFailed}
          description={ERR.changesFailedHint}
        />
      ) : null}

      <SectionCard title={CHANGES.title(items.length)}>
        <ResponsiveList
          items={isMobile ? pagedItems : items}
          rowKey={(row) => row.id}
          loading={changes.isLoading}
          renderCard={renderCard}
          empty={emptyState}
          footer={
            items.length > pageSize ? (
              <div style={{ display: "flex", justifyContent: "center", marginTop: 12 }}>
                <Pagination
                  simple
                  current={cardPage}
                  pageSize={pageSize}
                  total={items.length}
                  onChange={setCardPage}
                />
              </div>
            ) : null
          }
          table={
            <Table<ChangeEvent>
              size="small"
              rowKey="id"
              loading={changes.isLoading}
              dataSource={items}
              locale={{ emptyText: <>{emptyState}</> }}
              pagination={{
                pageSize: 50,
                showSizeChanger: false,
                showTotal: (total) => `共 ${total} 条`,
              }}
              columns={[
              {
                title: "类型",
                dataIndex: "event_type",
                width: 100,
                sorter: (a: ChangeEvent, b: ChangeEvent) => a.event_type.localeCompare(b.event_type),
                render: (value: ChangeEvent["event_type"]) => (
                  <StatusPill domain="change" value={value} size="sm" />
                ),
              },
              {
                title: "门店",
                dataIndex: "store_code",
                width: 190,
                render: (value: string, row: ChangeEvent) => (
                  <Link to={`/stores/${value}`}>{formatStoreLabel(value, row.store_name)}</Link>
                ),
              },
              {
                title: "货号",
                dataIndex: "style_code",
                width: 150,
                render: (value: string, row: ChangeEvent) =>
                  value ? (
                    <Link to={`/styles/${value}?store=${row.store_code}&size=${row.size_label}`}>
                      {value}
                    </Link>
                  ) : (
                    "—"
                  ),
              },
              {
                title: "尺码",
                dataIndex: "size_label",
                width: 80,
                sorter: (a: ChangeEvent, b: ChangeEvent) => a.size_label.localeCompare(b.size_label),
                render: (value: string) => formatSizeLabel(value),
              },
              {
                title: "有货状态变化",
                width: 180,
                render: (_: unknown, row: ChangeEvent) =>
                  row.before_status || row.after_status
                    ? `${stateLabel(row.before_status)} → ${stateLabel(row.after_status)}`
                    : "—",
              },
              {
                title: "实付价变化",
                width: 170,
                render: (_: unknown, row: ChangeEvent) =>
                  formatPriceChange(row.before_total_price, row.after_total_price),
              },
              {
                title: "促销",
                dataIndex: "promotions",
                render: (value: string[]) => formatPromotions(value),
              },
              {
                title: "探测时间",
                dataIndex: "checked_at",
                width: 150,
                defaultSortOrder: "descend",
                sorter: (a: ChangeEvent, b: ChangeEvent) => a.checked_at.localeCompare(b.checked_at),
                render: (value: string) => (
                  <Tooltip title={`相对时间：${formatRelative(value)}`}>{formatDate(value)}</Tooltip>
                ),
              },
            ]}
            />
          }
        />
      </SectionCard>
    </Space>
  );
}
