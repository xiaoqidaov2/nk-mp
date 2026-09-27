import { Alert, Card, Descriptions, Empty, Input, Space, Table, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { FreshnessBadge } from "@/components/FreshnessBadge";
import { availabilityLabel } from "@/components/StatusTag";
import { catalogApi, changeApi, matrixApi } from "@/data/api";
import type { ChangeEvent, Matrix, StoreOut } from "@/data/types";

export function StorePage() {
  const { storeCode = "" } = useParams();
  const [styleFilter, setStyleFilter] = useState("");

  const stores = useQuery<StoreOut[]>({ queryKey: ["stores"], queryFn: () => catalogApi.stores() });
  const styles = useQuery({ queryKey: ["styles"], queryFn: () => catalogApi.styles() });
  const changes = useQuery<ChangeEvent[]>({
    queryKey: ["changes", "store", storeCode],
    queryFn: () => changeApi.list({ days: 14 }),
  });

  const store = useMemo(
    () => (stores.data ?? []).find((item) => item.store_code === storeCode),
    [stores.data, storeCode],
  );

  const styleCodes = useMemo(() => {
    const fromChanges = (changes.data ?? [])
      .filter((item) => item.store_code === storeCode && item.style_code)
      .map((item) => item.style_code);
    const registered = (styles.data ?? []).map((item) => item.style_code);
    const all = Array.from(new Set([...fromChanges, ...registered]));
    if (!styleFilter.trim()) return all.slice(0, 12);
    const needle = styleFilter.trim().toUpperCase();
    return all.filter((code) => code.includes(needle)).slice(0, 24);
  }, [changes.data, styles.data, storeCode, styleFilter]);

  const matrix = useQuery<Matrix>({
    queryKey: ["matrix", styleCodes.join(","), storeCode],
    queryFn: () => matrixApi.get(styleCodes[0], [storeCode]),
    enabled: styleCodes.length > 0,
  });

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Typography.Title level={5} style={{ margin: 0 }}>
            {storeCode} · {store?.display_name || "未命名门店"}
          </Typography.Title>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}>
            <Descriptions.Item label="地址">{store?.address || "—"}</Descriptions.Item>
            <Descriptions.Item label="概念店类型">{store?.concept || "—"}</Descriptions.Item>
          </Descriptions>
          <Alert
            type="info"
            showIcon
            message="单店全量商品清单无法通过接口获得"
            description="本页仅展示已监控货号和满减专区预览池的结果，不代表门店全部在售商品。"
          />
        </Space>
      </Card>

      <Card
        size="small"
        title="本店已监控结果"
        extra={
          <Input
            size="small"
            placeholder="按货号筛选"
            style={{ width: 200 }}
            value={styleFilter}
            onChange={(event) => setStyleFilter(event.target.value)}
            allowClear
          />
        }
      >
        {styleCodes.length === 0 ? (
          <Empty description="本店暂无已监控结果" />
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <FreshnessBadge checkedAt={matrix.data?.last_checked_at} />
            <Typography.Text type="secondary">
              当前展示货号：{styleCodes[0]}（切换货号请使用下方变化记录或货号页）
            </Typography.Text>
            <Table
              size="small"
              rowKey={(row) => `${row.store_code}-${row.size_label}`}
              loading={matrix.isLoading}
              dataSource={matrix.data?.cells ?? []}
              pagination={{ pageSize: 20, hideOnSinglePage: true }}
              columns={[
                { title: "尺码", dataIndex: "size_label", width: 90 },
                {
                  title: "状态",
                  dataIndex: "availability",
                  width: 120,
                  render: (value: Matrix["cells"][number]["availability"]) => availabilityLabel(value),
                },
                {
                  title: "实付价",
                  dataIndex: "total_price",
                  width: 120,
                  render: (value: number | null) => (value != null ? `¥${value.toFixed(2)}` : "待补价"),
                },
                {
                  title: "促销",
                  dataIndex: "promotions",
                  render: (value: string[]) => (value.length ? value.join("；") : "—"),
                },
              ]}
            />
          </Space>
        )}
      </Card>

      <Card size="small" title={`本店近 14 天变化（${changes.data?.filter((item) => item.store_code === storeCode).length ?? 0} 条）`}>
        <Table<ChangeEvent>
          size="small"
          rowKey="id"
          loading={changes.isLoading}
          dataSource={(changes.data ?? []).filter((item) => item.store_code === storeCode)}
          locale={{ emptyText: <Empty description="本店暂无变化记录" /> }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={[
            {
              title: "类型",
              dataIndex: "event_type",
              width: 100,
              render: (value: string) => <Tag>{value}</Tag>,
            },
            {
              title: "货号",
              dataIndex: "style_code",
              render: (value: string) =>
                value ? <Link to={`/styles/${value}?store=${storeCode}`}>{value}</Link> : "—",
            },
            { title: "尺码", dataIndex: "size_label", width: 90, render: (v: string) => v || "—" },
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
