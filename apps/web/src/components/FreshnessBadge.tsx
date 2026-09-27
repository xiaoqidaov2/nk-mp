import { Tooltip } from "antd";

export function FreshnessBadge({ checkedAt }: { checkedAt: string | null | undefined }) {
  if (!checkedAt) {
    return <span style={{ color: "#8a5a08" }}>尚未探测</span>;
  }
  const when = new Date(checkedAt);
  const minutes = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000));
  const stale = minutes > 120;
  return (
    <Tooltip title={`绝对时间：${when.toLocaleString("zh-CN")}`}>
      <span style={{ color: stale ? "#8a5a08" : "#4b5563" }}>
        最近探测：{minutes < 1 ? "刚刚" : `${minutes} 分钟前`}
        {stale ? "（数据已陈旧）" : ""}
      </span>
    </Tooltip>
  );
}
