import { Card } from "antd";
import type { ReactNode } from "react";

import { LoadingBlock } from "./LoadingBlock";

export interface SectionCardProps {
  /** H3 16px/600 */
  title?: ReactNode;
  /** 卡片头下的 13px secondary */
  description?: ReactNode;
  /** 右上：图例按钮、复制 TSV、导出 */
  extra?: ReactNode;
  /** 用 LoadingBlock 而非 antd Skeleton */
  loading?: boolean;
  /** default true */
  bordered?: boolean;
  /** default true；表格类传 false（表格自带 padding） */
  padded?: boolean;
  bodyClassName?: string;
  children: ReactNode;
}

/** 卡片圆角 --radius-md(6)、box-shadow: none + 1px --border-color-secondary。 */
export function SectionCard({
  title,
  description,
  extra,
  loading = false,
  bordered = true,
  padded = true,
  bodyClassName,
  children,
}: SectionCardProps) {
  return (
    <Card
      variant={bordered ? "outlined" : "borderless"}
      className={bodyClassName}
      style={{ boxShadow: "none", borderRadius: "var(--radius-md)" }}
      styles={{ body: { padding: padded ? 16 : 0 } }}
      title={
        title ? (
          <div>
            <div>{title}</div>
            {description ? <div className="section-card__description">{description}</div> : null}
          </div>
        ) : undefined
      }
      extra={extra}
    >
      {loading ? <LoadingBlock variant="card" /> : children}
    </Card>
  );
}
