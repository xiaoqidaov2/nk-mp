import { Alert, Descriptions, Input, Space, Table, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { EmptyState } from "@/components/EmptyState";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatusPill } from "@/components/StatusPill";
import { TimeText } from "@/components/TimeText";
import {
  BTN,
  EMPTY,
  ERR,
  STORE_PAGE,
  formatPrice,
  formatPriceChange,
  formatPromotions,
  formatSizeLabel,
  formatStoreLabel,
  latestCheckedAt,
} from "@/copy";
import { catalogApi, changeApi, matrixApi } from "@/data/api";
import type { ChangeEvent, Matrix, MatrixCell, StoreOut } from "@/data/types";

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

  const storeChanges = (changes.data ?? []).filter((item) => item.store_code === storeCode);
  const cells = matrix.data?.cells ?? [];

  const resultCards = (
    <ResponsiveList
      items={cells}
      rowKey={(row) => `${row.store_code}-${row.size_label}`}
      loading={matrix.isLoading}
      empty={
        <EmptyState
          variant="no-data"
          description={EMPTY.storeResults}
          action={<Link to="/tasks/new">{BTN.newTask}</Link>}
          compact
        />
      }
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <span>{formatSizeLabel(row.size_label)}</span>
            <StatusPill domain="availability" value={row.availability} size="sm" />
          </div>
          <CardRow label="实付价">{formatPrice(row.total_price, row.availability)}</CardRow>
        </>
      )}
      table={
        <Table<MatrixCell>
          size="small"
          rowKey={(row) => `${row.store_code}-${row.size_label}`}
          loading={matrix.isLoading}
          dataSource={cells}
          locale={{ emptyText: EMPTY.storeResults }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          columns={[
            { title: "尺码", dataIndex: "size_label", width: 90, render: (value: string) => formatSizeLabel(value) },
            {
              title: "有货状态",
              dataIndex: "availability",
              width: 120,
              render: (value: MatrixCell["availability"]) => (
                <StatusPill domain="availability" value={value} size="sm" />
              ),
            },
            {
              title: "实付价",
              dataIndex: "total_price",
              width: 120,
              render: (value: number | null, row: MatrixCell) => formatPrice(value, row.availability),
            },
            {
              title: "标价",
              dataIndex: "price",
              width: 110,
              render: (value: number | null) => formatPrice(value),
            },
            {
              title: "促销",
              dataIndex: "promotions",
              render: (value: string[]) => formatPromotions(value),
            },
            {
              title: "探测时间",
              dataIndex: "checked_at",
              width: 130,
              render: (value: string | null) => <TimeText value={value} />,
            },
          ]}
        />
      }
    />
  );

  const changeCards = (
    <ResponsiveList
      items={storeChanges}
      rowKey={(row) => row.id}
      loading={changes.isLoading}
      empty={<EmptyState variant="no-data" description={EMPTY.storeChanges} compact />}
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <StatusPill domain="change" value={row.event_type} size="sm" />
            <Link to={`/styles/${row.style_code}?store=${storeCode}`}>{row.style_code}</Link>
          </div>
          <CardRow label="实付价">
            {formatPriceChange(row.before_total_price, row.after_total_price)}
          </CardRow>
        </>
      )}
      table={
        <Table<ChangeEvent>
          size="small"
          rowKey="id"
          loading={changes.isLoading}
          dataSource={storeChanges}
          locale={{ emptyText: EMPTY.storeChanges }}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
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
              title: "货号",
              dataIndex: "style_code",
              render: (value: string) =>
                value ? <Link to={`/styles/${value}?store=${storeCode}`}>{value}</Link> : "—",
            },
            { title: "尺码", dataIndex: "size_label", width: 90, render: (value: string) => formatSizeLabel(value) },
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
              render: (value: string) => <TimeText value={value} />,
            },
          ]}
        />
      }
    />
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title={formatStoreLabel(storeCode, store?.display_name)} />
      <NikeConnectionNotice action="读取本店数据" />

      {stores.isSuccess && !store ? (
        <Alert type="warning" showIcon message={ERR.storeNotFound} />
      ) : null}

      <SectionCard>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}>
            <Descriptions.Item label="地址">{store?.address || "—"}</Descriptions.Item>
            <Descriptions.Item label="概念店">{store?.concept || "—"}</Descriptions.Item>
          </Descriptions>
          <Alert type="info" showIcon message={STORE_PAGE.capability} />
        </Space>
      </SectionCard>

      <SectionCard
        title={STORE_PAGE.resultsTitle}
        extra={
          <Input
            size="small"
            placeholder={STORE_PAGE.filterPlaceholder}
            style={{ width: 200 }}
            value={styleFilter}
            onChange={(event) => setStyleFilter(event.target.value)}
            allowClear
          />
        }
      >
        {styleCodes.length === 0 ? (
          <EmptyState
            variant="no-data"
            description={EMPTY.storeResults}
            action={<Link to="/tasks/new">{BTN.newTask}</Link>}
            compact
          />
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <FreshnessBadge checkedAt={latestCheckedAt(matrix.data?.last_checked_at, cells)} />
            <Typography.Text type="secondary">
              {STORE_PAGE.currentStyle(styleCodes[0])}
            </Typography.Text>
            {resultCards}
          </Space>
        )}
      </SectionCard>

      <SectionCard title={STORE_PAGE.changesTitle(storeChanges.length)}>{changeCards}</SectionCard>
    </Space>
  );
}
