import { Alert, App as AntdApp, Button, Input, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { EmptyState } from "@/components/EmptyState";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { CardRow, ResponsiveList } from "@/components/MobileCardList";
import { NikeConnectionNotice } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/SectionCard";
import { StatusLegend, MatrixCellBadge } from "@/components/StatusTag";
import { StatusPill } from "@/components/StatusPill";
import { TimeText } from "@/components/TimeText";
import {
  BTN,
  EMPTY,
  ERR,
  MSG,
  STYLE_PAGE,
  formatCount,
  formatPrice,
  formatPromotions,
  formatSizeLabel,
  latestCheckedAt,
} from "@/copy";
import { matrixApi, scanApi, taskApi } from "@/data/api";
import type { Matrix, MatrixCell, MonitorTask } from "@/data/types";

export function StylePage() {
  const { styleCode = "" } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { message } = AntdApp.useApp();
  const { canWrite } = useAuth();
  const [sizeFilter, setSizeFilter] = useState("");
  const [inStockOnly, setInStockOnly] = useState(true);

  const matrix = useQuery<Matrix>({
    queryKey: ["matrix", styleCode],
    queryFn: () => matrixApi.get(styleCode.toUpperCase()),
    enabled: Boolean(styleCode),
  });

  const emptyResults = matrix.isSuccess && (matrix.data?.cells.length ?? 0) === 0;
  const tasks = useQuery<MonitorTask[]>({
    queryKey: ["tasks"],
    queryFn: () => taskApi.list(),
    enabled: emptyResults && canWrite,
  });

  const runNow = useMutation({
    mutationFn: (taskId: string) => scanApi.create({ task_id: taskId }),
    onSuccess: (job) => {
      message.success(MSG.checkStarted());
      navigate(`/scans/${job.id}`);
    },
    onError: () => message.error(ERR.checkStartFailed),
  });

  const highlightSize = searchParams.get("size") ?? "";
  const highlightStore = searchParams.get("store") ?? "";

  const filteredSizes = useMemo(() => {
    const sizes = matrix.data?.sizes ?? [];
    if (!sizeFilter.trim()) return sizes;
    const needle = sizeFilter.trim().toLowerCase();
    return sizes.filter((size) => size.toLowerCase().includes(needle));
  }, [matrix.data, sizeFilter]);

  const cellLookup = useMemo(() => {
    const map = new Map<string, MatrixCell>();
    for (const cell of matrix.data?.cells ?? []) {
      map.set(`${cell.store_code}|${cell.size_label}`, cell);
    }
    return map;
  }, [matrix.data]);

  if (matrix.isLoading) return <SectionCard loading>{null}</SectionCard>;
  if (matrix.isError || !matrix.data) {
    return (
      <Alert
        type="error"
        showIcon
        message={ERR.matrixFailed}
        description={ERR.matrixFailedHint}
      />
    );
  }
  const data = matrix.data;

  const rows = (() => {
    const list = data.stores.map((store) => {
      const cells = filteredSizes.map(
        (size) => cellLookup.get(`${store.store_code}|${size}`) ?? null,
      );
      const inStockCount = cells.filter((cell) => cell?.availability === "IN_STOCK").length;
      return { store, cells, inStockCount };
    });
    const visible = inStockOnly ? list.filter((row) => row.inStockCount > 0) : list;
    // 有货优先：在货尺码数多的门店排前面，其余按门店编号稳定排序。
    return [...visible].sort(
      (a, b) =>
        b.inStockCount - a.inStockCount ||
        a.store.store_code.localeCompare(b.store.store_code),
    );
  })();

  const taskWithStyle = (tasks.data ?? []).find((task) => task.style_codes.includes(data.style_code));
  const matrixEmpty = data.cells.length === 0;
  const sizeFilterEmpty = !matrixEmpty && filteredSizes.length === 0;
  // 默认「仅有货门店」不是用户筛选：整货号都没货时要给数据型空态，而不是"筛选无结果"。
  const anyInStock = data.cells.some((cell) => cell.availability === "IN_STOCK");
  const storeRowsEmpty = !matrixEmpty && !sizeFilterEmpty && rows.length === 0;

  const emptyState = matrixEmpty ? (
    <EmptyState
      variant="not-probed"
      description={EMPTY.matrixNoResults}
      action={
        taskWithStyle && canWrite ? (
          <Button
            type="primary"
            loading={runNow.isPending}
            onClick={() => runNow.mutate(taskWithStyle.id)}
          >
            {BTN.checkNow}
          </Button>
        ) : (
          <Link to="/tasks/new">{BTN.newTask}</Link>
        )
      }
      compact
    />
  ) : sizeFilterEmpty ? (
    <EmptyState
      variant="no-result"
      description={EMPTY.matrixFiltered(sizeFilter.trim())}
      action={<Button onClick={() => setSizeFilter("")}>{BTN.clearFilter}</Button>}
      compact
    />
  ) : storeRowsEmpty ? (
    anyInStock ? (
      <EmptyState
        variant="no-result"
        description={STYLE_PAGE.storeEmptyFiltered(sizeFilter.trim())}
        action={<Button onClick={() => setSizeFilter("")}>{BTN.clearFilter}</Button>}
        compact
      />
    ) : (
      // 整货号都没货：这是数据现状，不是"筛选无结果"，所以给数据型空态与「显示全部门店」。
      <EmptyState
        variant="no-data"
        description={STYLE_PAGE.storeEmptyAllOut(data.stores.length)}
        action={
          inStockOnly ? (
            <Button onClick={() => setInStockOnly(false)}>{STYLE_PAGE.showAllStores}</Button>
          ) : undefined
        }
        compact
      />
    )
  ) : null;

  const detailCards = (
    <ResponsiveList
      items={data.cells}
      rowKey={(row) => `${row.store_code}|${row.size_label}`}
      empty={<EmptyState variant="not-probed" description={EMPTY.matrixNoResults} compact />}
      renderCard={(row) => (
        <>
          <div className="mobile-card-list__row">
            <Link to={`/stores/${row.store_code}`}>{row.store_name || row.store_code}</Link>
            <StatusPill domain="availability" value={row.availability} size="sm" />
          </div>
          <CardRow label="尺码">{formatSizeLabel(row.size_label)}</CardRow>
          <CardRow label="实付价">{formatPrice(row.total_price, row.availability)}</CardRow>
        </>
      )}
      table={
        <Table<MatrixCell>
          size="small"
          rowKey={(row) => `${row.store_code}|${row.size_label}`}
          dataSource={data.cells}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          scroll={{ x: 720 }}
          columns={[
            {
              title: "门店",
              dataIndex: "store_name",
              width: 180,
              render: (value: string, row: MatrixCell) => (
                <Link to={`/stores/${row.store_code}`}>
                  {row.store_code}
                  {value ? ` · ${value}` : ""}
                </Link>
              ),
            },
            {
              title: "尺码",
              dataIndex: "size_label",
              width: 80,
              render: (value: string) => formatSizeLabel(value),
            },
            {
              title: "有货状态",
              dataIndex: "availability",
              width: 110,
              render: (value: MatrixCell["availability"]) => (
                <StatusPill domain="availability" value={value} size="sm" />
              ),
            },
            {
              title: "实付价",
              dataIndex: "total_price",
              width: 110,
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
              title: "最近探测",
              dataIndex: "checked_at",
              width: 150,
              render: (value: string | null) => <TimeText value={value} />,
            },
          ]}
        />
      }
    />
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader
        title={`${data.style_code}${data.title && data.title !== data.style_code ? ` · ${data.title}` : ""}`}
        meta={
          <>
            <FreshnessBadge checkedAt={latestCheckedAt(data.last_checked_at, data.cells)} />
            {data.structure_status !== "OK" ? (
              <Tag color="gold">{STYLE_PAGE.structureWarning}</Tag>
            ) : null}
          </>
        }
      />
      <NikeConnectionNotice action="读取该货号数据" />

      <SectionCard>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space wrap>
            <Input
              placeholder={STYLE_PAGE.filterPlaceholder}
              value={sizeFilter}
              onChange={(event) => setSizeFilter(event.target.value)}
              style={{ width: 200 }}
              allowClear
            />
            <Button onClick={() => setInStockOnly((prev) => !prev)}>
              {inStockOnly ? STYLE_PAGE.showAllStores : STYLE_PAGE.inStockOnly}
            </Button>
            <Typography.Text type="secondary">
              {STYLE_PAGE.countSummary(rows.length, filteredSizes.length)}
            </Typography.Text>
          </Space>
          <Typography.Text type="secondary">{STYLE_PAGE.stockFirstHint}</Typography.Text>
          <StatusLegend />
        </Space>
      </SectionCard>

      <SectionCard title={STYLE_PAGE.matrixTitle}>
        {emptyState ?? (
          <div className="table-scroll-x">
            <table className="matrix-table" style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr>
                  <th className="matrix-store-col" style={{ textAlign: "left", padding: 8 }}>
                    门店
                  </th>
                  {filteredSizes.map((size) => (
                    <th
                      key={size}
                      className={size === highlightSize ? "matrix-highlight-col" : undefined}
                      style={{ padding: "6px 4px" }}
                    >
                      {size}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.store.store_code}
                    className={
                      row.store.store_code === highlightStore ? "matrix-highlight-row" : undefined
                    }
                  >
                    <td className="matrix-store-col" style={{ padding: 8 }}>
                      <Link to={`/stores/${row.store.store_code}`}>
                        {row.store.store_code}
                        {row.store.store_name ? ` · ${row.store.store_name}` : ""}
                      </Link>
                      <div className="matrix-store-count">有货 {row.inStockCount} 码</div>
                    </td>
                    {row.cells.map((cell, index) => (
                      <td
                        key={`${row.store.store_code}-${filteredSizes[index]}`}
                        className={
                          filteredSizes[index] === highlightSize
                            ? "matrix-highlight-cell"
                            : undefined
                        }
                        style={{ textAlign: "center", padding: 3 }}
                      >
                        {cell ? (
                          <MatrixCellBadge
                            availability={cell.availability}
                            priceStatus={cell.price_status}
                            totalPrice={cell.total_price}
                            checkedAt={cell.checked_at}
                            trust={cell.trust}
                          />
                        ) : (
                          <span
                            className="matrix-cell matrix-cell--unknown"
                            title={STYLE_PAGE.matrixEmptyCell}
                          >
                            ?
                          </span>
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      <SectionCard
        title={STYLE_PAGE.detailTitle}
        extra={
          <Typography.Text type="secondary">
            共 {formatCount(data.cells.length)} 条
          </Typography.Text>
        }
      >
        {detailCards}
      </SectionCard>
    </Space>
  );
}
