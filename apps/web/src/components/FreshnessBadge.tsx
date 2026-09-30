import { Tooltip } from "antd";

import { formatRelative } from "@/copy";

export interface FreshnessBadgeProps {
  checkedAt?: string | null;
  /** 表格/矩阵：最早探测 */
  earliestAt?: string | null;
  /** 表格/矩阵：最晚探测（跨度 > 1h 时显示区间） */
  latestAt?: string | null;
  /** 与可信度徽标并排 */
  trust?: "TRUSTED" | "PARTIAL" | "UNTRUSTED";
  /** default 120（>2h 陈旧） */
  staleMinutes?: number;
  /** default 30 */
  freshMinutes?: number;
  /** default true：hover tooltip 绝对时间 */
  showAbsolute?: boolean;
  /** default "最近探测" */
  prefix?: string;
  /** default true */
  inline?: boolean;
  /** 点击打开“数据时间分布”popover */
  onClick?: () => void;
}

function hhmm(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
}

function absolute(value: string | null | undefined): string {
  if (!value) return "尚未探测";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "尚未探测";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

/** 文案恒为“最近探测/尚未探测”（禁止“实时”）。 */
export function FreshnessBadge({
  checkedAt,
  earliestAt,
  latestAt,
  trust,
  staleMinutes = 120,
  freshMinutes = 30,
  showAbsolute = true,
  prefix = "最近探测",
  inline = true,
  onClick,
}: FreshnessBadgeProps) {
  const latest = latestAt ?? checkedAt ?? null;
  const earliest = earliestAt ?? null;

  if (!latest) {
    return (
      <span
        style={{ color: "var(--color-stale)", display: inline ? "inline" : "block" }}
        data-trust={trust}
      >
        {prefix === "最近探测" ? "尚未探测" : `${prefix}：尚未探测`}
      </span>
    );
  }

  const latestTime = new Date(latest).getTime();
  const minutes = Math.max(0, Math.floor((Date.now() - latestTime) / 60_000));
  const color =
    minutes <= freshMinutes
      ? "var(--color-fresh)"
      : minutes > staleMinutes
        ? "var(--color-stale)"
        : "var(--color-text-secondary)";

  const spanMinutes =
    earliest && latest && !Number.isNaN(new Date(earliest).getTime())
      ? Math.floor((latestTime - new Date(earliest).getTime()) / 60_000)
      : 0;

  const text =
    spanMinutes > 60
      ? `${prefix} ${hhmm(earliest)} – ${hhmm(latest)}`
      : `${prefix}：${formatRelative(latest)}`;

  const tooltip = earliest
    ? `最早探测：${absolute(earliest)} · 最晚探测：${absolute(latest)}`
    : `绝对时间：${absolute(latest)}`;

  const node = (
    <span
      style={{ color, display: inline ? "inline" : "block", cursor: onClick ? "pointer" : undefined }}
      onClick={onClick}
      data-trust={trust}
    >
      {text}
    </span>
  );

  return showAbsolute ? <Tooltip title={tooltip}>{node}</Tooltip> : node;
}
