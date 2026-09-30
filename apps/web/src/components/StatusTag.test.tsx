import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  MatrixCellBadge,
  StatusLegend,
  authAttemptMeta,
  availabilityLabel,
  credentialStatusMeta,
  errorCodeText,
  eventTypeMeta,
  jobStatusMeta,
  storeStatusMeta,
} from "./StatusTag";

describe("MatrixCellBadge", () => {
  it("shows stock with the price when available", () => {
    render(
      <MatrixCellBadge availability="IN_STOCK" priceStatus="OK" totalPrice={384.45} />,
    );
    expect(screen.getByText(/¥384\.45/)).toBeInTheDocument();
  });

  it("marks in-stock cells without a price as pending, not free", () => {
    render(<MatrixCellBadge availability="IN_STOCK" priceStatus="MISSING" />);
    expect(screen.getByText(/待补价/)).toBeInTheDocument();
  });

  it("distinguishes unknown from out of stock", () => {
    render(<MatrixCellBadge availability="UNKNOWN" />);
    expect(screen.getByText(/未探测/)).toBeInTheDocument();
    expect(screen.queryByText(/无货/)).not.toBeInTheDocument();
  });

  it("renders untrusted cells as not-probed and hides their price", () => {
    render(
      <MatrixCellBadge
        availability="IN_STOCK"
        priceStatus="OK"
        totalPrice={384.45}
        trust="UNTRUSTED"
      />,
    );
    expect(screen.getByText(/未探测/)).toBeInTheDocument();
    expect(screen.queryByText(/¥384\.45/)).not.toBeInTheDocument();
  });

  it("renders every status in the legend", () => {
    render(<StatusLegend />);
    ["有货", "无货", "不售", "未探测", "无尺码表"].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });
});

describe("status maps", () => {
  it("maps every scan job status to one Chinese label", () => {
    expect(jobStatusMeta.QUEUED.label).toBe("排队中");
    expect(jobStatusMeta.RUNNING.label).toBe("进行中");
    expect(jobStatusMeta.SUCCEEDED.label).toBe("已完成");
    expect(jobStatusMeta.PARTIAL.label).toBe("部分完成");
    expect(jobStatusMeta.FAILED.label).toBe("失败");
    expect(jobStatusMeta.CANCELLED.label).toBe("已取消");
  });

  it("maps every store result status to one Chinese label", () => {
    expect(storeStatusMeta.PENDING.label).toBe("等待中");
    expect(storeStatusMeta.RUNNING.label).toBe("检查中");
    expect(storeStatusMeta.SUCCEEDED.label).toBe("已完成");
    expect(storeStatusMeta.FAILED.label).toBe("失败");
    expect(storeStatusMeta.UNKNOWN.label).toBe("结果不完整");
  });

  it("maps every change event to one Chinese label", () => {
    expect(eventTypeMeta.RESTOCK.label).toBe("补货");
    expect(eventTypeMeta.SOLD_OUT.label).toBe("售罄");
    expect(eventTypeMeta.PRICE_CHANGE.label).toBe("变价");
    expect(eventTypeMeta.NEW_ARRIVAL_AT_STORE.label).toBe("到店上新");
    expect(eventTypeMeta.SCAN_FAILED.label).toBe("检查失败");
    expect(eventTypeMeta.STRUCTURE_CHANGED.label).toBe("尺码表变化");
  });

  it("maps credential and attempt states to one Chinese label", () => {
    expect(credentialStatusMeta.NOT_CONNECTED.label).toBe("未连接");
    expect(credentialStatusMeta.UNASSIGNED.label).toBe("未连接");
    expect(credentialStatusMeta.ACTIVE.label).toBe("已连接（自动续期）");
    expect(credentialStatusMeta.EXPIRING.label).toBe("已连接（即将到期）");
    expect(credentialStatusMeta.NEEDS_AUTHORIZATION.label).toBe("需要重新验证");
    expect(credentialStatusMeta.REVOKED.label).toBe("已断开");
    expect(authAttemptMeta.SENDING.label).toBe("正在发送验证码…");
    expect(authAttemptMeta.CODE_SENT.label).toBe("请输入短信验证码");
    expect(authAttemptMeta.VERIFYING.label).toBe("正在验证…");
    expect(authAttemptMeta.CONNECTED.label).toBe("已连接");
    expect(authAttemptMeta.FAILED.label).toBe("验证未完成");
    expect(authAttemptMeta.EXPIRED.label).toBe("验证码已过期");
    expect(authAttemptMeta.CANCELLED.label).toBe("已取消");
    expect(authAttemptMeta.CHALLENGE_REQUIRED.label).toBe("需要额外验证");
  });

  it("labels every availability value in Chinese", () => {
    expect(availabilityLabel("IN_STOCK")).toBe("有货");
    expect(availabilityLabel("OUT_OF_STOCK")).toBe("无货");
    expect(availabilityLabel("NOT_CARRIED")).toBe("不售");
    expect(availabilityLabel("UNKNOWN")).toBe("未探测");
    expect(availabilityLabel("STRUCTURE_MISSING")).toBe("无尺码表");
  });

  it("translates store failure reasons and never leaks an unknown code as visible text", () => {
    expect(errorCodeText("EMPTY_COMPLETED")).toBe(
      "Nike 返回了空结果，这次不算查到，已按“未探测”处理。",
    );
    expect(errorCodeText("WAF_CHALLENGE")).toContain("风控");
    expect(errorCodeText("NIKE_CODE_INVALID")).toBe("验证码不正确，请检查短信后重新输入。");
    expect(errorCodeText("NIKE_AUTH_RATE_LIMITED", 42)).toBe("操作太频繁，请在 42 秒后重试。");
    expect(errorCodeText(null)).toBeNull();
    // 未知码返回 null，由调用方兜底为“系统原因”，原始码只进 tooltip。
    expect(errorCodeText("SOMETHING_ELSE")).toBeNull();
  });
});
