import {
  ApartmentOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  BlockOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  CloseCircleFilled,
  CloseCircleOutlined,
  DisconnectOutlined,
  EditOutlined,
  ExclamationCircleFilled,
  ExclamationCircleOutlined,
  HistoryOutlined,
  HourglassOutlined,
  LoadingOutlined,
  MailOutlined,
  MinusCircleFilled,
  MinusCircleOutlined,
  PauseCircleFilled,
  PlayCircleFilled,
  PlusCircleOutlined,
  QuestionCircleOutlined,
  SafetyCertificateOutlined,
  StopFilled,
  SwapOutlined,
  SyncOutlined,
  TableOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { createElement } from "react";
import type { ReactNode } from "react";

/** 状态域：一个域内的每个值 = 一个中文标签 + 一个颜色 + 一个图标（全站唯一）。 */
export type StatusDomain =
  | "availability"
  | "change"
  | "trust"
  | "task"
  | "credential"
  | "authAttempt"
  | "scanFailure"
  | "job"
  | "store";

export type StatusTone = "positive" | "warning" | "danger" | "neutral" | "info";

export interface StatusToken {
  /** 中文文案（矩阵状态文案不得改）。 */
  label: string;
  /** 图标（aria-hidden 由渲染组件负责）。 */
  icon: ReactNode;
  /** 前景色（仅允许 CSS 变量引用，禁止颜色字面量）。 */
  fg: string;
  bg: string;
  border: string;
  /** 45° 斜纹底色（仅 UNKNOWN 使用，与无货实底区分）。 */
  stripe?: string;
  /** 矩阵单元格内的 aria-hidden 符号：★ ✗ — ? ! */
  symbol?: string;
  hatched?: boolean;
  tone: StatusTone;
}

function icon(node: ReactNode): ReactNode {
  return createElement("span", { "aria-hidden": "true", className: "status-pill__icon" }, node);
}

const SUCCESS = {
  fg: "var(--color-success)",
  bg: "var(--color-success-bg)",
  border: "var(--color-success-border)",
};
const WARNING = {
  fg: "var(--color-warning)",
  bg: "var(--color-warning-bg)",
  border: "var(--color-warning-border)",
};
const DANGER = {
  fg: "var(--color-danger)",
  bg: "var(--color-danger-bg)",
  border: "var(--color-danger-border)",
};
const INFO = {
  fg: "var(--color-info)",
  bg: "var(--color-info-bg)",
  border: "var(--color-info-border)",
};
const NEUTRAL = {
  fg: "var(--color-neutral)",
  bg: "var(--color-neutral-bg)",
  border: "var(--color-neutral-border)",
};

/**
 * 单站唯一状态注册表。任何业务组件不得出现颜色字面量；
 * 新状态必须先在 STATUS 注册再使用。
 */
export const STATUS: Record<StatusDomain, Record<string, StatusToken>> = {
  availability: {
    IN_STOCK: {
      label: "有货",
      symbol: "★",
      icon: icon(createElement(CheckCircleFilled)),
      fg: "var(--state-in-fg)",
      bg: "var(--state-in-bg)",
      border: "var(--state-in-border)",
      tone: "positive",
    },
    OUT_OF_STOCK: {
      label: "无货",
      symbol: "✗",
      icon: icon(createElement(CloseCircleOutlined)),
      fg: "var(--state-out-fg)",
      bg: "var(--state-out-bg)",
      border: "var(--state-out-border)",
      tone: "neutral",
    },
    NOT_CARRIED: {
      label: "不售",
      symbol: "—",
      icon: icon(createElement(MinusCircleOutlined)),
      fg: "var(--state-notcarried-fg)",
      bg: "var(--state-notcarried-bg)",
      border: "var(--state-notcarried-border)",
      tone: "neutral",
    },
    UNKNOWN: {
      label: "未探测",
      symbol: "?",
      icon: icon(createElement(QuestionCircleOutlined)),
      fg: "var(--state-unknown-fg)",
      bg: "var(--state-unknown-bg)",
      stripe: "var(--state-unknown-stripe)",
      border: "var(--state-unknown-border)",
      hatched: true,
      tone: "neutral",
    },
    STRUCTURE_MISSING: {
      label: "无尺码表",
      symbol: "!",
      icon: icon(createElement(ExclamationCircleOutlined)),
      fg: "var(--state-nostruct-fg)",
      bg: "var(--state-nostruct-bg)",
      border: "var(--state-nostruct-border)",
      tone: "warning",
    },
  },

  change: {
    RESTOCK: {
      label: "补货",
      icon: icon(createElement(ArrowUpOutlined)),
      fg: "var(--event-restock-fg)",
      bg: "var(--event-restock-bg)",
      border: "var(--event-restock-border)",
      tone: "positive",
    },
    SOLD_OUT: {
      label: "售罄",
      icon: icon(createElement(ArrowDownOutlined)),
      fg: "var(--event-soldout-fg)",
      bg: "var(--event-soldout-bg)",
      border: "var(--event-soldout-border)",
      tone: "danger",
    },
    PRICE_CHANGE: {
      label: "变价",
      icon: icon(createElement(SwapOutlined)),
      fg: "var(--event-price-fg)",
      bg: "var(--event-price-bg)",
      border: "var(--event-price-border)",
      tone: "warning",
    },
    NEW_ARRIVAL_AT_STORE: {
      label: "到店上新",
      icon: icon(createElement(PlusCircleOutlined)),
      fg: "var(--event-new-fg)",
      bg: "var(--event-new-bg)",
      border: "var(--event-new-border)",
      tone: "info",
    },
    SCAN_FAILED: {
      label: "检查失败",
      icon: icon(createElement(WarningOutlined)),
      fg: "var(--event-scanfail-fg)",
      bg: "var(--event-scanfail-bg)",
      border: "var(--event-scanfail-border)",
      tone: "danger",
    },
    STRUCTURE_CHANGED: {
      label: "尺码表变化",
      icon: icon(createElement(TableOutlined)),
      fg: "var(--event-struct-fg)",
      bg: "var(--event-struct-bg)",
      border: "var(--event-struct-border)",
      tone: "warning",
    },
  },

  trust: {
    TRUSTED: {
      label: "结果可靠",
      icon: icon(createElement(SafetyCertificateOutlined)),
      ...SUCCESS,
      tone: "positive",
    },
    PARTIAL: {
      label: "部分结果",
      icon: icon(createElement(SafetyCertificateOutlined)),
      ...WARNING,
      tone: "warning",
    },
    UNTRUSTED: {
      label: "结果不可靠",
      icon: icon(createElement(WarningOutlined)),
      ...DANGER,
      tone: "danger",
    },
  },

  task: {
    ENABLED: {
      label: "启用",
      icon: icon(createElement(PlayCircleFilled)),
      ...SUCCESS,
      tone: "positive",
    },
    PAUSED: {
      label: "暂停",
      icon: icon(createElement(PauseCircleFilled)),
      ...WARNING,
      tone: "warning",
    },
    DRAFT: {
      label: "草稿",
      icon: icon(createElement(EditOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
    EXPIRED: {
      label: "已过期",
      icon: icon(createElement(HistoryOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
  },

  credential: {
    NOT_CONNECTED: {
      label: "未连接",
      icon: icon(createElement(DisconnectOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
    UNASSIGNED: {
      label: "未连接",
      icon: icon(createElement(DisconnectOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
    ACTIVE: {
      label: "已连接（自动续期）",
      icon: icon(createElement(CheckCircleFilled)),
      ...SUCCESS,
      tone: "positive",
    },
    EXPIRING: {
      label: "已连接（即将到期）",
      icon: icon(createElement(ClockCircleOutlined)),
      ...WARNING,
      tone: "warning",
    },
    NEEDS_AUTHORIZATION: {
      label: "需要重新验证",
      icon: icon(createElement(ExclamationCircleFilled)),
      ...DANGER,
      tone: "danger",
    },
    REVOKED: {
      label: "已断开",
      icon: icon(createElement(DisconnectOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
  },

  authAttempt: {
    SENDING: {
      label: "正在发送验证码…",
      icon: icon(createElement(LoadingOutlined)),
      ...INFO,
      tone: "info",
    },
    CODE_SENT: {
      label: "请输入短信验证码",
      icon: icon(createElement(MailOutlined)),
      ...INFO,
      tone: "info",
    },
    VERIFYING: {
      label: "正在验证…",
      icon: icon(createElement(SyncOutlined, { spin: true })),
      ...INFO,
      tone: "info",
    },
    CONNECTED: {
      label: "已连接",
      icon: icon(createElement(CheckCircleFilled)),
      ...SUCCESS,
      tone: "positive",
    },
    FAILED: {
      label: "验证未完成",
      icon: icon(createElement(CloseCircleFilled)),
      ...DANGER,
      tone: "danger",
    },
    EXPIRED: {
      label: "验证码已过期",
      icon: icon(createElement(ClockCircleOutlined)),
      ...WARNING,
      tone: "warning",
    },
    CANCELLED: {
      label: "已取消",
      icon: icon(createElement(StopFilled)),
      ...NEUTRAL,
      tone: "neutral",
    },
    CHALLENGE_REQUIRED: {
      label: "需要额外验证",
      icon: icon(createElement(ExclamationCircleFilled)),
      ...DANGER,
      tone: "danger",
    },
  },

  scanFailure: {
    AUTH_401_COOLDOWN: {
      label: "账号 401/冷却",
      icon: icon(createElement(HourglassOutlined)),
      ...WARNING,
      tone: "warning",
    },
    WAF_405_CHALLENGE: {
      label: "WAF 405 挑战",
      icon: icon(createElement(BlockOutlined)),
      ...DANGER,
      tone: "danger",
    },
    NETWORK_EOF: {
      label: "网络 TLS EOF",
      icon: icon(createElement(DisconnectOutlined)),
      ...DANGER,
      tone: "danger",
    },
    EMPTY_RESPONSE: {
      label: "空完成响应（不可信）",
      icon: icon(createElement(WarningOutlined)),
      ...DANGER,
      tone: "danger",
    },
    NO_GTIN_STRUCTURE: {
      label: "无 GTIN 结构（跳过）",
      icon: icon(createElement(ApartmentOutlined)),
      fg: "var(--state-nostruct-fg)",
      bg: "var(--state-nostruct-bg)",
      border: "var(--state-nostruct-border)",
      tone: "warning",
    },
  },

  job: {
    QUEUED: {
      label: "排队中",
      icon: icon(createElement(ClockCircleOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
    RUNNING: {
      label: "进行中",
      icon: icon(createElement(SyncOutlined, { spin: true })),
      ...INFO,
      tone: "info",
    },
    SUCCEEDED: {
      label: "已完成",
      icon: icon(createElement(CheckCircleFilled)),
      ...SUCCESS,
      tone: "positive",
    },
    PARTIAL: {
      label: "部分完成",
      icon: icon(createElement(MinusCircleFilled)),
      ...WARNING,
      tone: "warning",
    },
    FAILED: {
      label: "失败",
      icon: icon(createElement(CloseCircleFilled)),
      ...DANGER,
      tone: "danger",
    },
    CANCELLED: {
      label: "已取消",
      icon: icon(createElement(StopFilled)),
      ...NEUTRAL,
      tone: "neutral",
    },
  },

  store: {
    PENDING: {
      label: "等待中",
      icon: icon(createElement(ClockCircleOutlined)),
      ...NEUTRAL,
      tone: "neutral",
    },
    RUNNING: {
      label: "检查中",
      icon: icon(createElement(SyncOutlined, { spin: true })),
      ...INFO,
      tone: "info",
    },
    SUCCEEDED: {
      label: "已完成",
      icon: icon(createElement(CheckCircleFilled)),
      ...SUCCESS,
      tone: "positive",
    },
    FAILED: {
      label: "失败",
      icon: icon(createElement(CloseCircleFilled)),
      ...DANGER,
      tone: "danger",
    },
    UNKNOWN: {
      label: "结果不完整",
      icon: icon(createElement(ExclamationCircleFilled)),
      ...WARNING,
      tone: "warning",
    },
  },
};

/** 未知 domain/value 的中性兜底（同时 console.warn，不静默空白）。 */
export const NEUTRAL_STATUS_TOKEN: StatusToken = {
  label: "未知状态",
  icon: icon(createElement(QuestionCircleOutlined)),
  ...NEUTRAL,
  tone: "neutral",
};

export function statusToken(domain: StatusDomain, value: string): StatusToken | undefined {
  return STATUS[domain][value];
}

/** 未探测 / 结果不可靠的强制提示句（StatusPill tooltip 必填项）。 */
export const UNTRUSTED_TOOLTIP = "结果不可靠，已按“未探测”处理。";
