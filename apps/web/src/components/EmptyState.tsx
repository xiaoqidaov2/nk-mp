import type { ReactNode } from "react";

export type EmptyVariant =
  | "no-data"
  | "no-result"
  | "not-probed"
  | "error"
  | "offline"
  | "coming-soon";

export interface EmptyStateProps {
  /** 决定 SVG 与默认文案 */
  variant: EmptyVariant;
  title?: string;
  description?: ReactNode;
  /** 主 CTA Button */
  action?: ReactNode;
  secondaryAction?: ReactNode;
  /** true → 120→80px 图，用于卡内 */
  compact?: boolean;
  className?: string;
}

const DEFAULT_TITLE: Record<EmptyVariant, string> = {
  "no-data": "还没有数据",
  "no-result": "当前筛选无结果（清除筛选）",
  "not-probed": "从未探测过",
  error: "加载失败（重试）",
  offline: "离线数据（{时间}）",
  "coming-soon": "该功能仅电脑端可用",
};

function Illustration({ variant }: { variant: EmptyVariant }) {
  const base = {
    className: "empty-illustration",
    viewBox: "0 0 120 120",
    role: "img" as const,
    "aria-hidden": true,
    strokeLinecap: "round" as const,
    strokeWidth: 1.5,
  };
  switch (variant) {
    case "no-data":
      return (
        <svg {...base}>
          <rect
            x="24"
            y="40"
            width="72"
            height="52"
            rx="6"
            fill="var(--color-bg-layout)"
            stroke="var(--border-color)"
          />
          <path
            d="M24 62h20l6 8h20l6-8h20"
            fill="none"
            stroke="var(--border-color)"
            strokeLinejoin="round"
          />
          <circle
            cx="60"
            cy="30"
            r="10"
            fill="var(--color-primary-bg)"
            stroke="var(--color-primary-border)"
          />
          <path d="M56 30h8M60 26v8" stroke="var(--color-primary-text)" />
        </svg>
      );
    case "no-result":
      return (
        <svg {...base}>
          <circle
            cx="54"
            cy="54"
            r="22"
            fill="var(--color-bg-layout)"
            stroke="var(--color-text-tertiary)"
          />
          <path d="M70 70l14 14" stroke="var(--color-text-tertiary)" />
          <path d="M46 54h16" stroke="var(--color-text-tertiary)" />
        </svg>
      );
    case "not-probed":
      return (
        <svg {...base}>
          <circle cx="60" cy="60" r="28" fill="var(--color-bg-layout)" stroke="var(--border-color)" />
          <circle cx="60" cy="60" r="16" fill="none" stroke="var(--border-color)" />
          <path d="M60 60l20-14" stroke="var(--color-warning)" />
          <circle cx="60" cy="60" r="3" fill="var(--color-warning)" />
          <text
            x="60"
            y="88"
            textAnchor="middle"
            fontSize="14"
            fill="var(--color-warning)"
            stroke="none"
          >
            ?
          </text>
        </svg>
      );
    case "error":
      return (
        <svg {...base}>
          <rect
            x="26"
            y="50"
            width="26"
            height="20"
            rx="10"
            fill="var(--color-bg-layout)"
            stroke="var(--color-danger)"
          />
          <rect
            x="68"
            y="50"
            width="26"
            height="20"
            rx="10"
            fill="var(--color-bg-layout)"
            stroke="var(--color-danger)"
          />
          <path d="M52 60h6M62 60h6" stroke="var(--color-danger)" />
          <path d="M60 34v10" stroke="var(--color-danger)" />
          <path d="M56 40l4 4 4-4" fill="none" stroke="var(--color-danger)" />
        </svg>
      );
    case "offline":
      return (
        <svg {...base}>
          <path
            d="M38 76a14 14 0 0 1 1-28 18 18 0 0 1 34-4 14 14 0 0 1 9 32z"
            fill="var(--color-bg-layout)"
            stroke="var(--color-text-secondary)"
            strokeLinejoin="round"
          />
          <path d="M34 34l52 52" stroke="var(--color-text-secondary)" />
        </svg>
      );
    case "coming-soon":
      return (
        <svg {...base}>
          <rect
            x="26"
            y="34"
            width="68"
            height="46"
            rx="6"
            fill="var(--color-bg-layout)"
            stroke="var(--color-text-tertiary)"
          />
          <path d="M50 88h20" stroke="var(--color-text-tertiary)" />
          <path d="M60 80v8" stroke="var(--color-text-tertiary)" />
          <path d="M46 56h28" stroke="var(--color-text-tertiary)" />
        </svg>
      );
    default:
      return null;
  }
}

/** 内联 SVG 空态图；无图可显示时降级为文案。 */
export function EmptyState({
  variant,
  title,
  description,
  action,
  secondaryAction,
  compact = false,
  className,
}: EmptyStateProps) {
  return (
    <div className={["empty-state", className].filter(Boolean).join(" ")}>
      <div style={{ ["--empty-size" as string]: compact ? "80px" : "120px" }}>
        <Illustration variant={variant} />
      </div>
      <div className="empty-state__title">{title ?? DEFAULT_TITLE[variant]}</div>
      {description ? <div className="empty-state__description">{description}</div> : null}
      {action || secondaryAction ? (
        <div className="mobile-card-list__actions" style={{ justifyContent: "center" }}>
          {action}
          {secondaryAction}
        </div>
      ) : null}
    </div>
  );
}
