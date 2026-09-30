import { ArrowLeftOutlined } from "@ant-design/icons";
import { Breadcrumb, Button } from "antd";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export interface PageHeaderProps {
  /** H1 24px/600 */
  title: ReactNode;
  /** 14px secondary，最多 2 行 */
  subtitle?: ReactNode;
  breadcrumb?: { title: string; href?: string }[];
  back?: { onClick: () => void; label?: string };
  /** 一行：FreshnessBadge + TrustBadge + 数据截至 */
  meta?: ReactNode;
  /** 主/次操作，右侧 */
  extra?: ReactNode;
  /** default false；true 时 top: 56 */
  sticky?: boolean;
  /** default true，1px var(--border-color-secondary) */
  divider?: boolean;
  className?: string;
}

/** 全站页面只通过 PageHeader 表达 H1，禁止裸 <h1>。 */
export function PageHeader({
  title,
  subtitle,
  breadcrumb,
  back,
  meta,
  extra,
  sticky = false,
  divider = true,
  className,
}: PageHeaderProps) {
  return (
    <div className={["page-header", sticky ? "page-header--sticky" : "", className].filter(Boolean).join(" ")}>
      {breadcrumb && breadcrumb.length > 0 ? (
        <Breadcrumb
          items={breadcrumb.map((item) => ({
            title: item.href ? <Link to={item.href}>{item.title}</Link> : item.title,
          }))}
        />
      ) : null}
      <div className="page-header__row">
        <div className="page-header__main">
          {back ? (
            <Button
              type="text"
              icon={<ArrowLeftOutlined />}
              onClick={back.onClick}
              aria-label={back.label ?? "返回"}
            >
              {back.label ?? "返回"}
            </Button>
          ) : null}
          <div className="page-header__heading">
            <h1 className="page-header__title">{title}</h1>
            {subtitle ? <div className="page-header__subtitle">{subtitle}</div> : null}
          </div>
        </div>
        {extra ? <div className="page-header__extra">{extra}</div> : null}
      </div>
      {meta ? <div className="page-header__meta">{meta}</div> : null}
      {divider ? <div className="page-header__divider" /> : null}
    </div>
  );
}
