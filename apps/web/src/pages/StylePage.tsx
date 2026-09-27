import { Alert, Card, Empty, Input, Space, Table, Tag, Typography, Button } from "antd";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { FreshnessBadge } from "@/components/FreshnessBadge";
import { MatrixCellBadge, StatusLegend, availabilityLabel } from "@/components/StatusTag";
import { matrixApi } from "@/data/api";
import type { Matrix, MatrixCell } from "@/data/types";

export function StylePage() {
  const { styleCode = "" } = useParams();
  const [searchParams] = useSearchParams();
  const [sizeFilter, setSizeFilter] = useState("");
  const [inStockOnly, setInStockOnly] = useState(true);

  const matrix = useQuery<Matrix>({
    queryKey: ["matrix", styleCode],
    queryFn: () => matrixApi.get(styleCode.toUpperCase()),
    enabled: Boolean(styleCode),
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

  if (matrix.isLoading) return <Card loading />;
  if (matrix.isError) {
    return (
      <Alert
        type="error"
        showIcon
        message="无法加载货号矩阵"
        description={(matrix.error as Error).message}
      />
    );
  }
  const data = matrix.data as Matrix;

  const rows = data.stores
    .map((store) => {
      const cells = filteredSizes.map(
        (size) => cellLookup.get(`${store.store_code}|${size}`) ?? null,
      );
      const inStockCount = cells.filter((cell) => cell?.availability === "IN_STOCK").length;
      return { store, cells, inStockCount };
    })
    .filter((row) => (inStockOnly ? row.inStockCount > 0 : true));

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={12} wrap>
            <Typography.Title level={5} style={{ margin: 0 }}>
              {data.style_code}
              {data.title && data.title !== data.style_code ? ` · ${data.title}` : ""}
            </Typography.Title>
            <FreshnessBadge checkedAt={data.last_checked_at} />
            {data.structure_status !== "OK" ? (
              <Tag color="orange">部分门店缺少尺码结构</Tag>
            ) : null}
          </Space>
          <Space wrap>
            <Input
              placeholder="筛选尺码，例如 42"
              value={sizeFilter}
              onChange={(event) => setSizeFilter(event.target.value)}
              style={{ width: 200 }}
              allowClear
            />
            <Button onClick={() => setInStockOnly((prev) => !prev)}>
              {inStockOnly ? "显示全部门店" : "仅有货门店"}
            </Button>
            <Typography.Text type="secondary">
              共 {rows.length} 家门店、{filteredSizes.length} 个尺码
            </Typography.Text>
          </Space>
          <StatusLegend />
        </Space>
      </Card>

      <Card size="small" title="货号 × 门店 × 尺码">
        {rows.length === 0 ? (
          <Empty description="暂无探测结果，请先运行一次扫描" />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="matrix-table" style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr>
                  <th className="matrix-store-col" style={{ textAlign: "left", padding: 8 }}>
                    门店
                  </th>
                  {filteredSizes.map((size) => (
                    <th
                      key={size}
                      style={{
                        padding: "6px 4px",
                        background: size === highlightSize ? "#eaf1ff" : undefined,
                      }}
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
                    style={{
                      background:
                        row.store.store_code === highlightStore ? "#f2f8ff" : undefined,
                    }}
                  >
                    <td className="matrix-store-col" style={{ padding: 8 }}>
                      {row.store.store_code} · {row.store.store_name || "未命名门店"}
                      <div style={{ color: "#6b7280", fontSize: 11 }}>
                        有货 {row.inStockCount} 码
                      </div>
                    </td>
                    {row.cells.map((cell, index) => (
                      <td
                        key={`${row.store.store_code}-${filteredSizes[index]}`}
                        style={{
                          textAlign: "center",
                          padding: 3,
                          background:
                            filteredSizes[index] === highlightSize ? "#f7fbff" : undefined,
                        }}
                      >
                        {cell ? (
                          <MatrixCellBadge
                            availability={cell.availability}
                            priceStatus={cell.price_status}
                            totalPrice={cell.total_price}
                            checkedAt={cell.checked_at}
                          />
                        ) : (
                          <span
                            className="matrix-cell matrix-cell--unknown"
                            title="该门店没有这个尺码的结构或尚未探测"
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
      </Card>

      <Card size="small" title="明细（含价格与促销）">
        <Table<MatrixCell>
          size="small"
          rowKey={(row) => `${row.store_code}|${row.size_label}`}
          dataSource={data.cells}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          columns={[
            { title: "门店", dataIndex: "store_name", width: 180 },
            { title: "尺码", dataIndex: "size_label", width: 90 },
            {
              title: "状态",
              dataIndex: "availability",
              width: 110,
              render: (value: MatrixCell["availability"]) => availabilityLabel(value),
            },
            {
              title: "实付价",
              dataIndex: "total_price",
              width: 110,
              render: (value: number | null) => (value != null ? `¥${value.toFixed(2)}` : "待补价"),
            },
            {
              title: "标价",
              dataIndex: "price",
              width: 110,
              render: (value: number | null) => (value != null ? `¥${value.toFixed(2)}` : "—"),
            },
            {
              title: "促销",
              dataIndex: "promotions",
              render: (value: string[]) => (value.length ? value.join("；") : "—"),
            },
            {
              title: "最近探测",
              dataIndex: "checked_at",
              width: 170,
              render: (value: string | null) =>
                value ? new Date(value).toLocaleString("zh-CN") : "—",
            },
          ]}
        />
      </Card>
    </Space>
  );
}
