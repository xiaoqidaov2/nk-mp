import { describe, expect, it } from "vitest";

import * as copy from "./copy";

/** 递归收集 copy.ts 导出的所有文案（含模板函数用示例参数产出的句子）。 */
function collectStrings(value: unknown, out: string[] = [], depth = 0): string[] {
  if (depth > 6) return out;
  if (typeof value === "string") {
    out.push(value);
    return out;
  }
  if (typeof value === "function") {
    try {
      const produced = (value as (...args: unknown[]) => unknown)("示例", "$1.00");
      collectStrings(produced, out, depth + 1);
    } catch {
      // 无法用示例参数调用的函数不展开。
    }
    return out;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectStrings(item, out, depth + 1));
    return out;
  }
  if (value && typeof value === "object") {
    Object.values(value as Record<string, unknown>).forEach((item) =>
      collectStrings(item, out, depth + 1),
    );
  }
  return out;
}

const exportedText = collectStrings(copy).join("\n");

describe("copy.ts", () => {
  it("never contains a banned word in its exported copy", () => {
    // 计划验收的逐词清单（§1.3）：任何一个都必须为 0 命中。
    expect(exportedText).not.toMatch(/任务、|扫描、|采集、|观测、|租户、|配额、|凭证、|授权、|服务方后台/);
    // 独立术语级禁用词（否定句式与规格明确保留的句子除外）。
    const rules: { word: RegExp; allow?: RegExp }[] = [
      { word: /扫描/ },
      { word: /采集/ },
      { word: /观测/ },
      { word: /租户/, allow: /租户不可用/ },
      { word: /配额/ },
      { word: /审核请求/ },
      { word: /凭证/ },
      { word: /服务方后台/ },
      { word: /实时/ },
      { word: /SSE/ },
      { word: /websocket/i },
      { word: /错误码/ },
      { word: /授权/, allow: /Nike 授权已失效/ },
    ];
    for (const rule of rules) {
      if (!rule.word.test(exportedText)) continue;
      const allowedOnly = rule.allow
        ? exportedText
            .split("\n")
            .filter((line) => rule.word.test(line))
            .every((line) => rule.allow?.test(line))
        : false;
      expect(allowedOnly, `禁用词命中：${rule.word}`).toBe(true);
    }
  });

  it("never leaks an English status enum into user-facing copy", () => {
    const enums =
      /(IN_STOCK|OUT_OF_STOCK|NOT_CARRIED|STRUCTURE_MISSING|QUEUED|RUNNING|SUCCEEDED|PARTIAL|FAILED|CANCELLED|RESTOCK|SOLD_OUT|PRICE_CHANGE|NEW_ARRIVAL_AT_STORE|SCAN_FAILED|STRUCTURE_CHANGED|TRUSTED|UNTRUSTED|ACTIVE|EXPIRING|REVOKED)/;
    expect(exportedText).not.toMatch(enums);
  });

  it("keeps the critical confirmation sentences verbatim", () => {
    const tpl = copy.MODALS.deleteTask("测试任务");
    expect(tpl.title).toBe("删除这个监控任务？");
    expect(tpl.content).toContain("测试任务");
    expect(tpl.content).toContain("此操作无法撤销。");
    expect(tpl.okText).toBe("删除任务");
    expect(copy.MODALS.disconnectNike().okText).toBe("断开连接");
    expect(copy.EMPTY.taskList).toBe("还没有监控任务。创建后按周期自动检查指定货号与门店。");
    expect(copy.EMPTY.readonlyTasks).toBe("你当前是只读成员，可以查看结果但不能新建或修改任务。");
  });

  it("formats prices, times and counts in the clerk-facing way", () => {
    expect(copy.formatPrice(1234.5)).toBe("¥1,234.50");
    expect(copy.formatPrice(null, "IN_STOCK")).toBe("待补价");
    expect(copy.formatPrice(null, "OUT_OF_STOCK")).toBe("—");
    expect(copy.formatPrice(0, "IN_STOCK")).toBe("待补价");
    expect(copy.formatPriceChange(899, null)).toBe("¥899.00 → —");
    expect(copy.formatPriceChange(699, 699)).toBe("¥699.00");
    expect(copy.formatCadence(1800)).toBe("30 分钟");
    expect(copy.formatCadence(86400)).toBe("24 小时");
    expect(copy.formatCount(1024)).toBe("1024");
    expect(copy.formatCount(12345)).toBe("12,345");
    expect(copy.formatQuota(128, 500)).toBe("今日查询 128 / 500 次");
    expect(copy.formatStoreLabel("SH001", "上海南京东路店")).toBe("SH001 · 上海南京东路店");
    expect(copy.formatStoreLabel("SH001", "")).toBe("SH001");
    expect(copy.formatScanId("a1b2c3d4e5f6")).toBe("#a1b2c3d4");
    expect(copy.formatPromotions(["满1000减200", "会员9折"])).toBe("满1000减200；会员9折");
    expect(copy.formatPromotions([])).toBe("—");
    expect(copy.resendLabel(57)).toBe("57 秒后可重新获取");
    expect(copy.resendLabel(0)).toBe("重新获取验证码");
  });
});

describe("loginErrorText", () => {
  it("maps stable backend details and escalates after three failures", () => {
    expect(copy.loginErrorText("邮箱或密码不正确", 1)).toBe("邮箱或密码不正确，请重新输入。");
    expect(copy.loginErrorText("邮箱或密码不正确", 3)).toContain("忘记密码请联系管理员重置。");
    expect(copy.loginErrorText("租户不可用", 1)).toBe("你所在的公司账号已停用，请联系管理员。");
    expect(copy.loginErrorText("something else", 1)).toBe(
      "操作没有完成，请稍后重试；若仍失败，请把本页截图发给管理员。",
    );
  });
});
