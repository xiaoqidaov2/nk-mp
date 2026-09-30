import { Tooltip } from "antd";

import { formatDateTimeSeconds, formatRelative } from "@/copy";

/** 相对时间 + hover 绝对时间（列表/详情统一用法）。 */
export function TimeText({
  value,
  absolute = true,
}: {
  value?: string | null;
  absolute?: boolean;
}) {
  if (!value) return <>—</>;
  const relative = formatRelative(value);
  if (!absolute) return <>{relative}</>;
  return <Tooltip title={`绝对时间：${formatDateTimeSeconds(value)}`}>{relative}</Tooltip>;
}
