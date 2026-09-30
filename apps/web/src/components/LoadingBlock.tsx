import type { CSSProperties } from "react";

export interface LoadingBlockProps {
  variant: "table" | "card" | "stat" | "chart" | "matrix" | "form" | "text";
  /** table default 8；text default 3 */
  rows?: number;
  /** matrix default 8 */
  columns?: number;
  /** chart default 240 */
  height?: number;
  /** default "加载中" */
  ariaLabel?: string;
}

/** 禁止全屏 Spin；用骨架保持布局不跳动（表格行高 38px 与真实行一致）。 */
export function LoadingBlock({
  variant,
  rows,
  columns = 8,
  height = 240,
  ariaLabel = "加载中",
}: LoadingBlockProps) {
  const rowCount = rows ?? (variant === "text" ? 3 : variant === "table" ? 8 : 4);
  const style =
    variant === "matrix"
      ? ({ ["--loading-columns" as string]: String(columns) } as CSSProperties)
      : variant === "chart"
        ? ({ ["--loading-height" as string]: `${height}px` } as CSSProperties)
        : undefined;

  return (
    <div
      className={`loading-block loading-block--${variant}`}
      style={style}
      role="status"
      aria-busy="true"
      aria-label={ariaLabel}
    >
      {Array.from({ length: rowCount }).map((_, index) => (
        <div key={index} className="loading-block__row" />
      ))}
    </div>
  );
}
