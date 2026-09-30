import { Tooltip } from "antd";
import type { ReactNode } from "react";

import { NEUTRAL_STATUS_TOKEN, statusToken } from "./statusTokens";
import type { StatusDomain } from "./statusTokens";

export interface StatusPillProps {
  domain: StatusDomain;
  /** 如 "IN_STOCK" | "RESTOCK" | "ACTIVE" */
  value: string;
  /** default "md"：sm=12px/20px 高，md=13px/24px 高 */
  size?: "sm" | "md";
  showIcon?: boolean;
  showLabel?: boolean;
  bordered?: boolean;
  /** 如 ¥? 角标、价格文本 */
  extra?: ReactNode;
  /** 必填语义解释（未探测/不可信/无结构 必须给） */
  tooltip?: ReactNode;
  className?: string;
  title?: string;
}

/**
 * 一个状态一个色 + 一个图标；同状态跨页面渲染完全相同的色与图标。
 * 颜色全部来自 statusTokens（CSS 变量），组件内不出现颜色字面量。
 */
export function StatusPill({
  domain,
  value,
  size = "md",
  showIcon = true,
  showLabel = true,
  bordered = true,
  extra,
  tooltip,
  className,
  title,
}: StatusPillProps) {
  const token = statusToken(domain, value);
  if (!token) {
    // 开发期可见，避免未知状态静默空白。
    console.warn(`StatusPill: unknown status "${domain}/${value}"`);
  }
  const effective = token ?? NEUTRAL_STATUS_TOKEN;
  const background = effective.hatched
    ? `repeating-linear-gradient(45deg, ${effective.bg} 0 4px, ${effective.stripe ?? effective.bg} 4px 8px)`
    : effective.bg;

  const pill = (
    <span
      className={["status-pill", `status-pill--${size}`, className].filter(Boolean).join(" ")}
      style={{
        color: effective.fg,
        background,
        borderColor: bordered ? effective.border : "transparent",
      }}
      title={title}
    >
      {showIcon ? <span className="status-pill__icon" aria-hidden="true">{effective.icon}</span> : null}
      {showLabel ? <span>{effective.label}</span> : null}
      {extra !== undefined && extra !== null ? <span className="status-pill__extra">{extra}</span> : null}
    </span>
  );

  return tooltip ? <Tooltip title={tooltip}>{pill}</Tooltip> : pill;
}
