import type { Key, ReactNode } from "react";

import { useIsMobile } from "@/hooks/useIsMobile";
import { EmptyState } from "./EmptyState";
import { LoadingBlock } from "./LoadingBlock";

export interface MobileCardListProps<T> {
  items: T[];
  rowKey: (item: T) => Key;
  renderCard: (item: T, index: number) => ReactNode;
  /** 默认 <EmptyState variant="no-data" compact /> */
  empty?: ReactNode;
  /** 渲染 3 个骨架卡 */
  loading?: boolean;
  /** default 3 */
  skeletonCount?: number;
  /** "加载更多" / 分页 */
  footer?: ReactNode;
  className?: string;
}

/** ≤767px 的卡片列表：表格 → 卡片折叠容器。 */
export function MobileCardList<T>({
  items,
  rowKey,
  renderCard,
  empty,
  loading = false,
  skeletonCount = 3,
  footer,
  className,
}: MobileCardListProps<T>) {
  if (loading) {
    return (
      <div className={["mobile-card-list", className].filter(Boolean).join(" ")}>
        {Array.from({ length: skeletonCount }).map((_, index) => (
          <div key={index} className="mobile-card-list__item">
            <LoadingBlock variant="card" rows={2} />
          </div>
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return <>{empty ?? <EmptyState variant="no-data" compact />}</>;
  }

  return (
    <div className={["mobile-card-list", className].filter(Boolean).join(" ")}>
      {items.map((item, index) => (
        <div key={rowKey(item)} className="mobile-card-list__item">
          {renderCard(item, index)}
        </div>
      ))}
      {footer ? <div className="mobile-card-list__footer">{footer}</div> : null}
    </div>
  );
}

export interface ResponsiveListProps<T> extends Omit<MobileCardListProps<T>, "renderCard"> {
  renderCard: (item: T, index: number) => ReactNode;
  /** 桌面端表格 DOM */
  table: ReactNode;
  /** default 767 */
  breakpoint?: number;
}

export function ResponsiveList<T>({
  table,
  breakpoint = 767,
  ...cardProps
}: ResponsiveListProps<T>) {
  const isMobile = useIsMobile(breakpoint);
  if (!isMobile) return <>{table}</>;
  return <MobileCardList<T> {...cardProps} />;
}

/** 卡片内一行“标签: 值”。 */
export function CardRow({
  label,
  children,
}: {
  label: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mobile-card-list__row">
      <span className="mobile-card-list__label">{label}</span>
      <span className="mobile-card-list__value">{children}</span>
    </div>
  );
}
