import { Card, Empty, Segmented, Space, Table, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { changeApi } from "@/data/api";
import type { ChangeEvent } from "@/data/types";

const FILTERS = [
  { label: "全部", value: "" },
  { label: "补货", value: "RESTOCK" },
  { label: "售罄", value: "SOLD_OUT" },
  { label: "变价", value: "PRICE_CHANGE" },
  { label: "到店上新", value: "NEW_ARRIVAL_AT_STORE" },
  { label: "扫描失败", value: "SCAN_FAILED" },
  { label: "结构变化", value: "STRUCTURE_CHANGED" },
];

const TYPE_LABEL: Record<ChangeEvent["event_type"], string> = {
  RESTOCK: "补货",
  SOLD_OUT: "售罄",
  PRICE_CHANGE: "变价",
  NEW_ARRIVAL_AT_STORE: "到店上新",
  SCAN_FAILED: "扫描失败",
  STRUCTURE_CHANGED: "结构变化",
};

function priceText(value: number | null): string {
  return value != null ? `¥${value.toFixed(2)}` : "—";
}

export function ChangesPage() {
  const [filter, setFilter] = useState("");
  const changes = useQuery<ChangeEvent[]>({
    queryKey: ["changes", filter],
    queryFn: () => changeApi.list({ event_type: filter, days: 30 }),
  });

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Segmented
            options={FILTERS}
            value={filter}
            onChange={(value) => setFilter(String(value))}
          />
          <span style={{ color: "#4b5563", fontSize: 12 }}>
            仅在结果为可信观测时才记录变化：未探测、不可信、缺少尺码结构不会产生补货或售罄结论。
          </span>
        </Space>
      </Card>

      <Card size="small" title={`变化流水（近 30 天，共 ${changes.data?.length ?? 0} 条）`}>
        <Table<ChangeEvent>
          size="small"
          rowKey="id"
          loading={changes.isLoading}
          dataSource={changes.data ?? []}
          locale={{ emptyText: <Empty description="暂无变化记录" /> }}
          pagination={{ pageSize: 25, hideOnSinglePage: true }}
          columns={[
            {
              title: "类型",
              dataIndex: "event_type",
              width: 96,
              render: (value: ChangeEvent["event_type"]) => <Tag>{TYPE_LABEL[value]}</Tag>,
            },
            {
              title: "门店",
              dataIndex: "store_code",
              width: 200,
              render: (value: string, row: ChangeEvent) => (
                <Link to={`/stores/${value}`}>
                  {value} · {row.store_name || "未命名门店"}
                </Link>
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
            { title: "尺码", dataIndex: "size_label", width: 80, render: (v: string) => v || "—" },
            {
              title: "状态变化",
              width: 170,
              render: (_: unknown, row: ChangeEvent) =>
                row.before_status || row.after_status
                  ? `${row.before_status ?? "—"} → ${row.after_status ?? "—"}`
                  : "—",
            },
            {
              title: "价格",
              width: 170,
              render: (_: unknown, row: ChangeEvent) =>
                `${priceText(row.before_total_price)} → ${priceText(row.after_total_price)}`,
            },
            {
              title: "促销",
              dataIndex: "promotions",
              render: (value: string[]) => (value.length ? value.join("；") : "—"),
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
