import type {
  Availability,
  ChangeEvent,
  NikeAuthAttemptState,
  NikeCredentialStatus,
  PriceStatus,
  ScanJob,
  ScanStore,
  Trust,
} from "@/data/types";

import { StatusPill } from "./StatusPill";
import { STATUS } from "./statusTokens";
import type { StatusToken } from "./statusTokens";

/** 矩阵单元格类名：颜色全部来自 global.css 的 CSS 变量（§1.7 五态）。 */
const CELL_CLASS: Record<Availability, string> = {
  IN_STOCK: "matrix-cell--in",
  OUT_OF_STOCK: "matrix-cell--out",
  NOT_CARRIED: "matrix-cell--not-carried",
  UNKNOWN: "matrix-cell--unknown",
  STRUCTURE_MISSING: "matrix-cell--missing",
};

const LABELS: Record<Availability, { text: string; symbol: string; className: string }> = {
  IN_STOCK: {
    text: STATUS.availability.IN_STOCK.label,
    symbol: STATUS.availability.IN_STOCK.symbol ?? "★",
    className: CELL_CLASS.IN_STOCK,
  },
  OUT_OF_STOCK: {
    text: STATUS.availability.OUT_OF_STOCK.label,
    symbol: STATUS.availability.OUT_OF_STOCK.symbol ?? "✗",
    className: CELL_CLASS.OUT_OF_STOCK,
  },
  NOT_CARRIED: {
    text: STATUS.availability.NOT_CARRIED.label,
    symbol: STATUS.availability.NOT_CARRIED.symbol ?? "—",
    className: CELL_CLASS.NOT_CARRIED,
  },
  UNKNOWN: {
    text: STATUS.availability.UNKNOWN.label,
    symbol: STATUS.availability.UNKNOWN.symbol ?? "?",
    className: CELL_CLASS.UNKNOWN,
  },
  STRUCTURE_MISSING: {
    text: STATUS.availability.STRUCTURE_MISSING.label,
    symbol: STATUS.availability.STRUCTURE_MISSING.symbol ?? "!",
    className: CELL_CLASS.STRUCTURE_MISSING,
  },
};

export function availabilityLabel(value: Availability): string {
  return LABELS[value]?.text ?? value;
}

interface Props {
  availability: Availability;
  priceStatus?: PriceStatus;
  totalPrice?: number | null;
  checkedAt?: string | null;
  /** UNTRUSTED 一律按“未探测”显示，不显示价格（§3.5）。 */
  trust?: Trust;
}

/**
 * Status is conveyed by symbol, text and background so it never relies on
 * colour alone, and "unknown" is visually distinct from "out of stock".
 */
export function MatrixCellBadge({ availability, priceStatus, totalPrice, checkedAt, trust }: Props) {
  const effective: Availability = trust === "UNTRUSTED" ? "UNKNOWN" : availability;
  const meta = LABELS[effective] ?? LABELS.UNKNOWN;
  const pendingPrice = effective === "IN_STOCK" && priceStatus !== "OK";
  const priceText =
    effective === "IN_STOCK" && priceStatus === "OK" && totalPrice !== null && totalPrice !== undefined
      ? `¥${totalPrice.toFixed(2)}`
      : pendingPrice
        ? "待补价"
        : "";
  const title = checkedAt ? `最近探测：${new Date(checkedAt).toLocaleString("zh-CN")}` : "尚未探测";

  return (
    <span
      className={`matrix-cell ${meta.className}`}
      title={title}
      style={pendingPrice ? { borderStyle: "dashed" } : undefined}
    >
      <span aria-hidden="true">{meta.symbol}</span> {priceText || meta.text}
      {pendingPrice ? <span aria-hidden="true"> ¥?</span> : null}
    </span>
  );
}

export function StatusLegend() {
  return (
    <div className="status-legend">
      {Object.entries(LABELS).map(([key, meta]) => (
        <span key={key}>
          <span className={`matrix-cell ${meta.className}`} aria-hidden="true">
            {meta.symbol}
          </span>{" "}
          {meta.text}
        </span>
      ))}
      <span>★ 有货仍需以门店实际为准；价格为促销后实付</span>
      <span className="status-legend__trust">
        <StatusPill domain="trust" value="TRUSTED" size="sm" />
        <StatusPill domain="trust" value="PARTIAL" size="sm" />
        <StatusPill domain="trust" value="UNTRUSTED" size="sm" />
        <span>结果不可靠的探测按“未探测”显示，也不会生成变化记录。</span>
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 状态唯一映射（页面禁止内联映射；测试对此逐值断言）                    */
/* ------------------------------------------------------------------ */

export const jobStatusMeta = STATUS.job as Record<ScanJob["status"], StatusToken>;
export const storeStatusMeta = STATUS.store as Record<ScanStore["status"], StatusToken>;
export const eventTypeMeta = STATUS.change as Record<ChangeEvent["event_type"], StatusToken>;
export const credentialStatusMeta = STATUS.credential as Record<
  NikeCredentialStatus | "UNASSIGNED",
  StatusToken
>;
export const authAttemptMeta = STATUS.authAttempt as Record<NikeAuthAttemptState, StatusToken>;

/* ------------------------------------------------------------------ */
/* 失败原因：error_code → 中文句（界面永不出 code，原始码只进 tooltip）  */
/* ------------------------------------------------------------------ */

/** Nike 连接验证错误码（NikeAuthErrorCode，16 个）。 */
const NIKE_ERROR_TEXT: Record<string, string> = {
  NIKE_AUTH_NOT_AVAILABLE: "Nike 短信验证暂时不可用，请稍后再试。",
  NIKE_AUTH_IN_PROGRESS: "已经有一条验证正在进行。请完成或取消后，再获取新的验证码。",
  NIKE_AUTH_SESSION_MISMATCH: "页面登录状态已变化，请重新获取验证码。",
  NIKE_AUTH_REQUEST_CONFLICT: "本次请求与上一条验证冲突，请点“更换手机号”后重新获取验证码。",
  NIKE_SMS_SEND_UNCERTAIN:
    "短信可能已发出，为避免重复发送，请先查看手机短信；1 分钟后仍没收到，再点“重新获取验证码”。",
  NIKE_VERIFY_UNCERTAIN:
    "验证结果未确认，请不要重复提交。请稍后查看本页状态；仍未连接时重新获取验证码。",
  NIKE_CHALLENGE_REQUIRED:
    "Nike 要求额外安全验证，网页端无法完成。请换一台常用手机重试，或联系管理员。",
  NIKE_AUTH_CONTRACT_CHANGED:
    "Nike 登录方式已变更，暂时无法自动完成。请把本页截图发给管理员。",
  NIKE_CODE_INVALID: "验证码不正确，请检查短信后重新输入。",
  NIKE_CODE_EXPIRED: "验证码已过期，请点“重新获取验证码”。",
  NIKE_AUTH_RATE_LIMITED: "操作太频繁，请稍后再试。",
  NIKE_PHONE_CONNECT_UNAVAILABLE:
    "这个手机号已连接到其他账号，不能重复连接。请改用本人未被占用的手机号。",
  NIKE_CREDENTIAL_REQUIRED: "需要先连接 Nike 账号才能继续。请到账户页完成验证。",
  NIKE_CREDENTIAL_CHANGED: "账号连接已变化，请重新验证后再操作。",
  NIKE_REAUTH_REQUIRED: "Nike 授权已失效，请重新验证账号。未验证前检查不会更新数据。",
  NIKE_UPSTREAM_UNAVAILABLE: "Nike 服务暂时不可用，请稍后重试。",
};

/** 检查失败错误码（门店级 error_code）。 */
const SCAN_ERROR_TEXT: Record<string, string> = {
  EMPTY_COMPLETED: "Nike 返回了空结果，这次不算查到，已按“未探测”处理。",
  POLL_TIMEOUT: "等待 Nike 返回结果超时。",
  WAF_CHALLENGE: "被 Nike 风控拦截，本次未取到数据。",
  AUTH_REQUIRED: "Nike 授权已失效，本次未取到数据。",
  TRANSPORT_ERROR: "网络中断，本次未取到数据。",
  BATCH_SUSPECT: "返回的商品数量不完整，本次结果不采用。",
  ITEM_QUANTITY_LIMIT: "单次查询的商品数量超出上限，请减少货号后重试。",
  STRUCTURE_MISSING: "这家店没有该货号的尺码表，无法判断各尺码。",
  UNEXPECTED_STATUS: "Nike 返回了无法识别的状态，本次未取到数据。",
  UNKNOWN_ERROR_CODE: "出现未知问题，本次未取到数据。请把本页截图发给管理员。",
  BUDGET_EXHAUSTED: "今日额度已用完，剩余门店未检查。",
  THROTTLED: "被 Nike 限速，本次未取到数据。",
};

/**
 * error_code → 文员可读句。未知码返回 null（界面兜底显示“系统原因”，
 * 原始码只出现在 tooltip）。
 */
export function errorCodeText(
  code: string | null | undefined,
  retryAfterSeconds?: number | null,
): string | null {
  if (!code) return null;
  if (code === "NIKE_AUTH_RATE_LIMITED") {
    return retryAfterSeconds
      ? `操作太频繁，请在 ${retryAfterSeconds} 秒后重试。`
      : NIKE_ERROR_TEXT.NIKE_AUTH_RATE_LIMITED;
  }
  return NIKE_ERROR_TEXT[code] ?? SCAN_ERROR_TEXT[code] ?? null;
}
