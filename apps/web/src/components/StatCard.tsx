import type { KeyboardEvent, ReactNode } from "react";

import { LoadingBlock } from "./LoadingBlock";

const TONE_COLOR: Record<NonNullable<StatCardProps["tone"]>, string> = {
  default: "var(--color-text)",
  restock: "var(--event-restock-fg)",
  soldout: "var(--event-soldout-fg)",
  price: "var(--event-price-fg)",
  new: "var(--event-new-fg)",
  activity: "var(--color-primary-text)",
};

export interface StatCardProps {
  /** 13px secondary */
  label: string;
  /** 28px/600 tabular-nums */
  value: ReactNode;
  /** 14px secondary */
  unit?: string;
  delta?: { value: number; direction: "up" | "down" | "flat"; suffix?: string };
  /** 右下 12px caption，如 "数据截至 12:04" */
  hint?: ReactNode;
  tone?: "default" | "restock" | "soldout" | "price" | "new" | "activity";
  /** 14px 图标置于 label 前 */
  icon?: ReactNode;
  loading?: boolean;
  /** 有 onClick 时渲染为可点击卡（hover 阴影 --shadow-1） */
  onClick?: () => void;
  /** 数据源异常时置灰，不整页报错 */
  disabled?: boolean;
}

export function StatCard({
  label,
  value,
  unit,
  delta,
  hint,
  tone = "default",
  icon,
  loading = false,
  onClick,
  disabled = false,
}: StatCardProps) {
  const clickable = Boolean(onClick) && !disabled;
  const className = [
    "stat-card",
    clickable ? "stat-card--clickable" : "",
    disabled ? "stat-card--disabled" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const onKeyDown = clickable
    ? (event: KeyboardEvent<HTMLDivElement>) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick?.();
        }
      }
    : undefined;

  const deltaColor =
    delta?.direction === "up"
      ? "var(--color-success)"
      : delta?.direction === "down"
        ? "var(--color-danger)"
        : "var(--color-text-secondary)";

  return (
    <div
      className={className}
      onClick={clickable ? onClick : undefined}
      onKeyDown={onKeyDown}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <div className="stat-card__label">
        {icon}
        <span>{label}</span>
      </div>
      {loading ? (
        <LoadingBlock variant="stat" />
      ) : (
        <>
          <div className="stat-card__value-row">
            <span className="stat-card__value" style={{ color: TONE_COLOR[tone ?? "default"] }}>
              {value}
            </span>
            {unit ? <span className="stat-card__unit">{unit}</span> : null}
          </div>
          {delta ? (
            <div className="stat-card__delta" style={{ color: deltaColor }}>
              {delta.direction === "up" ? "↑" : delta.direction === "down" ? "↓" : "—"}
              {Math.abs(delta.value)}
              {delta.suffix ?? ""}
            </div>
          ) : null}
          {hint ? <div className="stat-card__hint">{hint}</div> : null}
        </>
      )}
    </div>
  );
}
