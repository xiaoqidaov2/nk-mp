import { Button, Progress, Space, Typography } from "antd";

import { CATALOG, ERR, formatDate } from "@/copy";
import { errorCodeText } from "@/components/StatusTag";
import type { NikeBootstrapOut } from "@/data/types";

export interface CatalogProgressProps {
  plan: NikeBootstrapOut;
  onRetry?: () => void;
  /** 卡内使用的小号环形进度；弹窗内使用大号。 */
  compact?: boolean;
}

/**
 * 门店目录预加载进度（环形）。数据归属当前账号：只显示"已缓存 N 家门店"，
 * 不暗示平台共享目录。
 */
export function CatalogProgress({ plan, onRetry, compact = false }: CatalogProgressProps) {
  const percent = Math.round((plan.progress ?? 0) * 100);

  if (plan.state === "SUCCEEDED") {
    return (
      <Typography.Text type="secondary">
        {CATALOG.cached(plan.store_count)}
        {plan.updated_at ? ` · 更新于 ${formatDate(plan.updated_at)}` : ""}
      </Typography.Text>
    );
  }

  if (plan.state === "FAILED") {
    const reason = errorCodeText(plan.error_code) ?? ERR.commonFailure;
    return (
      <Space direction={compact ? "horizontal" : "vertical"} size={8} wrap>
        <Typography.Text type="warning">
          {CATALOG.failed}：{reason}
        </Typography.Text>
        {onRetry ? (
          <Button size="small" onClick={onRetry}>
            {CATALOG.retry}
          </Button>
        ) : null}
      </Space>
    );
  }

  const ring = (
    <Progress
      type="circle"
      percent={percent}
      size={compact ? 28 : 132}
      strokeWidth={compact ? 10 : 6}
    />
  );
  const stage = plan.stage || CATALOG.loading;

  if (compact) {
    return (
      <Space size={8}>
        {ring}
        <Typography.Text type="secondary">{stage}</Typography.Text>
      </Space>
    );
  }
  return (
    <Space direction="vertical" size={8} align="center" style={{ width: "100%" }}>
      {ring}
      <Typography.Text type="secondary">{stage}</Typography.Text>
    </Space>
  );
}
