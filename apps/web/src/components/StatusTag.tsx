import type { Availability, PriceStatus } from "@/data/types";

const LABELS: Record<Availability, { text: string; symbol: string; className: string }> = {
  IN_STOCK: { text: "有货", symbol: "★", className: "matrix-cell--in" },
  OUT_OF_STOCK: { text: "无货", symbol: "✗", className: "matrix-cell--out" },
  NOT_CARRIED: { text: "不售", symbol: "—", className: "matrix-cell--not-carried" },
  UNKNOWN: { text: "未探测", symbol: "?", className: "matrix-cell--unknown" },
  STRUCTURE_MISSING: { text: "无尺码结构", symbol: "!", className: "matrix-cell--missing" },
};

export function availabilityLabel(value: Availability): string {
  return LABELS[value]?.text ?? value;
}

interface Props {
  availability: Availability;
  priceStatus?: PriceStatus;
  totalPrice?: number | null;
  checkedAt?: string | null;
}

/**
 * Status is conveyed by symbol, text and background so it never relies on
 * colour alone, and "unknown" is visually distinct from "out of stock".
 */
export function MatrixCellBadge({ availability, priceStatus, totalPrice, checkedAt }: Props) {
  const meta = LABELS[availability] ?? LABELS.UNKNOWN;
  const priceText =
    priceStatus === "OK" && totalPrice !== null && totalPrice !== undefined
      ? `¥${totalPrice.toFixed(2)}`
      : availability === "IN_STOCK"
        ? "待补价"
        : "";
  const title = checkedAt ? `最近探测：${new Date(checkedAt).toLocaleString("zh-CN")}` : "尚未探测";

  return (
    <span className={`matrix-cell ${meta.className}`} title={title}>
      <span aria-hidden="true">{meta.symbol}</span> {priceText || meta.text}
    </span>
  );
}

export function StatusLegend() {
  return (
    <div className="status-legend">
      {Object.entries(LABELS).map(([key, meta]) => (
        <span key={key}>
          <span className={`matrix-cell ${meta.className}`} aria-hidden="true">
            {meta.symbol}
          </span>{" "}
          {meta.text}
        </span>
      ))}
      <span>★ 有货仍需以门店实际为准；价格为促销后实付</span>
    </div>
  );
}
