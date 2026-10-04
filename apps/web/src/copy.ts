/**
 * 全站唯一文案常量文件（文员向）。
 *
 * 规则：术语一义一词；界面不得出现英文枚举；句子不含 HTTP 状态码/堆栈/原始 detail。
 * 格式函数按规格 §2.6 实现（Asia/Shanghai 时区、今天/昨天相对格式、价格千分位等）。
 */
import type { Availability } from "@/data/types";

const TZ = "Asia/Shanghai";

/* ------------------------------------------------------------------ */
/* 导航（§4.1）                                                        */
/* ------------------------------------------------------------------ */

export const NAV_ITEMS = [
  { key: "/dashboard", label: "概览" },
  { key: "/tasks", label: "监控任务" },
  { key: "/changes", label: "变化流水" },
  { key: "/account", label: "账户与额度" },
  { key: "/ops", label: "管理后台" },
] as const;

export const APP_NAME = "门店监控台";
export const UNOFFICIAL_TAG = "非官方，仅信息聚合";
export const FOOTER_NOTE =
  "数据来自公开渠道聚合，与品牌方无关联；单店全部在售商品不可得，页面仅展示已监控货号与促销预览商品。";

/* ------------------------------------------------------------------ */
/* 按钮动词表（§2.1）                                                  */
/* ------------------------------------------------------------------ */

export const BTN = {
  view: "查看",
  open: "打开",
  newTask: "新建监控任务",
  createTask: "创建任务",
  checkNow: "立即检查",
  retryFailedStores: "仅重试失败门店",
  enable: "启用",
  pause: "暂停",
  deleteTask: "删除任务",
  getCode: "获取验证码",
  verifyConnect: "验证并连接",
  changePhone: "更换手机号",
  cancel: "取消",
  disconnect: "断开连接",
  reauth: "重新验证/更换账号",
  syncStores: "同步门店清单",
  filter: "筛选",
  logout: "退出登录",
  back: "返回",
  prevStep: "上一步",
  nextStep: "下一步",
  add: "添加",
  selectAll: "全选",
  clear: "清空",
  refresh: "刷新",
  refreshPage: "刷新页面",
  viewAll: "查看全部",
  clearFilter: "清除筛选",
  gotIt: "我知道了",
  connectNike: "连接 Nike 账号",
  viewChanges: "查看变化流水",
  later: "以后再说",
} as const;

/* ------------------------------------------------------------------ */
/* 确认弹窗模板（§2.2）                                                */
/* ------------------------------------------------------------------ */

export interface ModalTemplate {
  title: string;
  content: string;
  okText: string;
  cancelText: string;
}

export const MODALS = {
  deleteTask: (name: string): ModalTemplate => ({
    title: "删除这个监控任务？",
    content: `删除后不再按周期检查「${name}」。已产生的检查结果和变化记录会保留。此操作无法撤销。`,
    okText: BTN.deleteTask,
    cancelText: BTN.cancel,
  }),
  disconnectNike: (): ModalTemplate => ({
    title: "断开 Nike 账号连接？",
    content: "断开后所有检查会停止，直到你重新验证。已有的检查结果保留。",
    okText: BTN.disconnect,
    cancelText: BTN.cancel,
  }),
  changeAccount: (): ModalTemplate => ({
    title: "更换账号会重建库存基线",
    content:
      "旧账号未完成的检查会结束；新账号首次检查只建立基线，不会产生补货或售罄提醒。",
    okText: BTN.gotIt,
    cancelText: BTN.back,
  }),
  pauseTask: (): ModalTemplate => ({
    title: "暂停这个监控任务？",
    content: "暂停后不再按周期检查，直到你重新启用。",
    okText: BTN.pause,
    cancelText: BTN.cancel,
  }),
  syncStores: (): ModalTemplate => ({
    title: "立即同步门店清单？",
    content: "同步会使用你当前的 Nike 连接发起查询，计入今日额度。",
    okText: BTN.syncStores,
    cancelText: BTN.cancel,
  }),
  retryStores: (count: number): ModalTemplate => ({
    title: `重试 ${count} 家未完成门店？`,
    content: "只重跑失败或结果不完整的门店，已成功的门店不会重复检查。",
    okText: BTN.retryFailedStores,
    cancelText: BTN.cancel,
  }),
} as const;

/* ------------------------------------------------------------------ */
/* 成功／失败提示（§2.3）                                              */
/* ------------------------------------------------------------------ */

export const MSG = {
  taskCreated: (cadence: string) => `监控任务已创建，将按「${cadence}」自动检查。`,
  taskCreatedChecking: () => "监控任务已创建，正在检查第 1 家门店…",
  taskEnabled: (name: string, nextRun: string) => `已启用「${name}」，下次检查时间 ${nextRun}。`,
  taskPaused: (name: string) => `已暂停「${name}」。`,
  taskDeleted: (name: string) => `已删除「${name}」。`,
  checkStarted: () => "已开始检查，正在跳转到进度页…",
  retriedStores: (count: number) => `已重新检查 ${count} 家门店。`,
  codeSent: (phone: string, mmss: string) => `验证码已发送到 ${phone}，${mmss} 内有效。`,
  connected: () => "Nike 账号已连接，接下来可以创建监控任务。",
  catalogCached: (count: number) => `已缓存 ${count} 家门店数据。`,
  disconnected: () => "已断开 Nike 账号连接。",
  storesSynced: (count: number) => `门店清单已更新，共 ${count} 家。`,
  progressResumed: () => "进度自动更新已恢复。",
  genericSuccess: () => "操作已完成。",
  genericFailure: () =>
    "操作没有完成，请稍后重试；若仍失败，请把本页截图发给管理员。",
  networkFailure: () => "网络连接失败，请检查网络后重试。",
} as const;

/* ------------------------------------------------------------------ */
/* 空态（§2.4）                                                        */
/* ------------------------------------------------------------------ */

export const EMPTY = {
  overviewFirst: "还没有开始监控。先连接 Nike 账号，再创建一个监控任务。",
  overviewNoTasks: "还没有监控任务。创建后系统会按你设置的频率自动检查。",
  overviewRecentChanges: "最近两天没有库存或价格变化。",
  taskList: "还没有监控任务。创建后按周期自动检查指定货号与门店。",
  scanList: "还没有检查记录。创建任务并立即检查后，这里会显示结果。",
  readonlyTasks: "你当前是只读成员，可以查看结果但不能新建或修改任务。",
  wizardNoStyles: "请先添加至少 1 个货号。",
  wizardStoreHint: "不选门店＝检查全部已登记门店；以后新登记的门店自动加入。",
  taskScans: "这个任务还没有检查记录。",
  scanStoresWaiting: "正在等待第 1 家门店返回结果…",
  scanStoresNone: "本次没有门店结果。",
  storeResults: "这家店还没有已监控货号的结果。先在监控任务里加入货号，再运行一次检查。",
  storeChanges: "这家店近 14 天没有库存或价格变化。",
  matrixNoResults: "这个货号还没有探测结果。运行一次检查后即可看到各门店尺码。",
  matrixFiltered: (term: string) => `没有符合「${term}」的尺码。`,
  changesNone: "近 30 天没有变化记录。只有结果可靠的探测才会生成记录。",
  changesFiltered: (typeLabel: string) => `近 30 天没有「${typeLabel}」记录。`,
  accountNotConnected: "还没有连接 Nike 账号。请用本人手机号完成验证。",
  opsTenants: "暂无公司记录。",
} as const;

/* ------------------------------------------------------------------ */
/* 错误提示（§2.5，文员可执行动作）                                     */
/* ------------------------------------------------------------------ */

export const ERR = {
  loginBadCredentials: "邮箱或密码不正确，请重新输入。",
  loginBadCredentialsHint: "忘记密码请联系管理员重置。",
  loginTenantDisabled: "你所在的公司账号已停用，请联系管理员。",
  network: "网络连接失败，请检查网络后重试。",
  commonFailure: "操作没有完成，请稍后重试；若仍失败，请把本页截图发给管理员。",
  forbidden: "你当前是只读成员，不能执行这个操作。",
  sessionExpired: "登录状态已过期，请重新登录。",
  budgetExhausted: "今日额度已用完，剩余门店未检查。明天自动恢复，不用操作。",
  streamInterrupted: "进度自动更新已中断，正在改为每 3 秒刷新一次。数据仍在更新，无需操作。",
  streamUnavailable: "进度更新暂时不可用，请稍后手动刷新页面。",
  streamUnrecognized: "收到的进度数据无法识别，已改为自动刷新。",
  notConnected: (action: string) =>
    `需要先连接 Nike 账号才能${action}。请到账户与额度页用本人手机号完成验证。`,
  taskListFailed: "任务列表加载失败，请刷新页面重试。",
  checkStartFailed: "检查没有启动，请稍后重试。",
  taskCreateFailed: "任务没有创建成功",
  overviewFailed: "概览加载失败",
  overviewFailedHint: "无法获取最新概况，请刷新页面重试。",
  matrixFailed: "无法加载这个货号的结果",
  matrixFailedHint: "请稍后重试；若一直失败，请刷新页面。",
  changesFailed: "变化记录加载失败",
  changesFailedHint: "请刷新页面重试。",
  accountFailed: "账号信息加载失败",
  accountFailedHint: "刷新已有账号信息，请刷新页面重试。",
  taskNotFound: "这个任务不存在，或你没有权限查看。",
  scanNotFound: "这次检查不存在，或你没有权限查看。",
  storeNotFound: "没有找到这家店，可能门店清单还没同步。",
  opsSyncFailed: "门店清单没有更新，请稍后重试。",
} as const;

export const LOGIN_SUBTITLE = "请使用公司分配的账号登录。账号由管理员创建，忘记密码请联系管理员。";

export function loginErrorText(detail: string | undefined, failedAttempts: number): string {
  const text = detail ?? "";
  if (text.includes("租户不可用")) return ERR.loginTenantDisabled;
  if (text.includes("邮箱或密码不正确")) {
    return failedAttempts >= 3
      ? `${ERR.loginBadCredentials}${ERR.loginBadCredentialsHint}`
      : ERR.loginBadCredentials;
  }
  return ERR.commonFailure;
}

/* ------------------------------------------------------------------ */
/* 逐页文案（§4.2–4.13）                                               */
/* ------------------------------------------------------------------ */

export const DASHBOARD = {
  connectionNone: "还没有连接 Nike 账号，检查不会更新数据。",
  connectionNoneAction: "去账户页完成手机验证",
  reauthRequired: "Nike 授权已失效，检查不会更新数据。",
  failedToday: (count: number) =>
    `今天的检查有 ${count} 次未完全成功，可在检查详情里只重试失败门店。`,
  statActiveTasks: "运行中的监控任务",
  statMonitoredStyles: "已监控货号",
  statMonitoredStores: "已有结果的门店",
  statRestock: "今日补货",
  statSoldOut: "今日售罄",
  statPriceChange: "今日变价",
  statNewArrival: "今日到店上新",
  statRunningJobs: "进行中检查",
  recentChanges: "近两日变化",
  capabilityLink: "查看数据能力说明",
} as const;

export interface GuideStep {
  title: string;
  description: string;
  action: string;
  disabledTooltip?: string;
}

export const GUIDE: { storageKey: string; dismiss: string; steps: GuideStep[] } = {
  storageKey: "nike_mp_guide_dismissed",
  dismiss: "以后再说",
  steps: [
    {
      title: "连接 Nike 账号",
      description: "用你本人的手机号完成短信验证，系统才能读取库存和价格。这一步只需要 1 分钟。",
      action: BTN.connectNike,
    },
    {
      title: "创建第一个监控任务",
      description: "选好要盯的货号和门店，系统会按你设置的频率自动检查。",
      action: BTN.newTask,
      disabledTooltip: "先完成第 1 步",
    },
    {
      title: "查看结果",
      description: "检查完成后，在“变化流水”看补货、售罄和变价。",
      action: BTN.viewChanges,
    },
  ],
};

export const TASK_LIST = {
  connectFirstTooltip: "需要先连接 Nike 账号，点这里去账户页",
  disabledNoStyles: "任务还没有货号，先编辑任务",
  disabledNoStores: "任务还没有门店，先编辑任务",
  disabledNotConnected: "需要先连接 Nike 账号",
  disabledReadonly: "只读成员不能发起检查",
  scanCountLabel: (done: number, total: number) => `${done} / ${total}`,
  manualCheck: "手动检查",
} as const;

export const CATALOG = {
  /** 门店目录按用户缓存：没有缓存时不要再让人去猜。 */
  emptyHint:
    "还没有门店数据。连接 Nike 账号后，系统会用你自己的账号自动获取并缓存门店清单，只属于你的账号。",
  afterConnect: "连接成功，正在用你自己的账号获取并缓存门店数据。",
  cached: (count: number) => `已缓存 ${count} 家门店`,
  loading: "正在获取门店数据…",
  failed: "门店数据没有获取成功",
  retry: "重新获取",
} as const;

export const WIZARD = {
  steps: ["选择货号和门店", "设置频率并确认"],
  styleLabel: "货号",
  stylePlaceholder: "粘贴货号，用空格、逗号或换行分隔，例如 DN3707-202 IO5031-600",
  styleSelectPlaceholder: "或从已登记货号中选择",
  styleCount: (count: number) => `共 ${count} 个货号。`,
  storeLabel: "门店",
  storePlaceholder: "选择门店，不选＝全部已登记门店",
  taskNameLabel: "任务名称",
  frequencyLabel: "检查频率",
  frequencyNote:
    "按所选间隔开始新一轮检查；门店越多一轮耗时越长，实际间隔会顺延（上一轮结束前不会叠加）。",
  notifyRestock: "补货提醒",
  notifySoldOut: "售罄提醒",
  notifyPriceChange: "变价提醒",
  runImmediately: "创建后立即检查一次",
  summaryLabel: "确认信息",
  summaryStyles: (count: number) => `${count} 个`,
  summaryStoresAll: "全部已登记门店",
  summaryStores: (count: number) => `${count} 家`,
  quotaNote:
    "每次检查会对每个货号 × 每家门店发起一次查询，计入今日额度。实际次数取决于各门店的尺码表和价格，暂无法提前估算。",
} as const;

export const ACCOUNT = {
  roleLabel: "角色",
  roleDescriptions: {
    ops_admin: "可管理后台与门店同步，并拥有全部查看能力。",
    ops_operator: "可执行门店同步，并拥有全部查看能力。",
    tenant_admin: "可新建、修改、删除本公司的监控任务。",
    tenant_viewer: "只能查看结果，不能新建或修改监控任务。",
  } as Record<string, string>,
  quotaTitle: "今日额度",
  quotaUsed: (used: string, budget: string) => `已使用 ${used} / ${budget} 次查询`,
  quotaNote:
    "额度由公司共享，同时受 Nike 风控限制。接近上限时，系统会优先保证已监控货号的有货检查，价格可能暂时显示“待补价”。",
  capabilityTitle: "数据能力说明",
  capabilityRows: [
    { item: "全国门店清单", available: "可获取，含地址与变更" },
    { item: "指定货号的门店尺码有货情况", available: "可获取，显示最近探测时间" },
    { item: "门店价与促销后实付价", available: "可获取，无货时没有价格" },
    { item: "一家店的全部在售商品和件数", available: "无法获取，本平台不提供" },
  ],
} as const;

export const PUSH = {
  cardTitle: "消息推送",
  cardNote: "补货、售罄、变价会推送到你配置的渠道；每次检查只发一条汇总消息。",
  empty: "还没有配置推送渠道。配置后，监控任务的提醒会发送到你的手机或群。",
  addChannel: "添加渠道",
  platformLabel: "平台",
  nameLabel: "渠道名称（可留空）",
  saveChannel: "保存渠道",
  sendTest: "发送测试",
  testOk: "测试消息已发送。",
  testFailed: (reason: string) => `测试失败：${reason}`,
  remove: "删除",
  removeConfirmTitle: "删除这个推送渠道？",
  removeConfirmBody: "删除后不再向它发送提醒，已发送的记录会保留（标为“已删除的渠道”）。",
  removeConfirmOk: "删除渠道",
  retry: "重发",
  channelRemoved: "已删除的渠道",
  stateSent: "已发送",
  statePending: "等待重试",
  stateFailed: "发送失败",
  lastOk: (time: string) => `最近发送 ${time}`,
  recentTitle: "最近推送",
  recentEmpty: "还没有推送记录。",
  dingtalkTitle: "钉钉企业内部机器人（可选）",
  dingtalkOptionalTag: "只收提醒可跳过",
  dingtalkConfiguredTag: "已配置",
  dingtalkNote:
    "只接收补货/售罄/变价提醒的话，用上面的「钉钉群机器人」渠道就够了，本节可以跳过。这里用于在钉钉里直接给平台发指令（如「检查」「额度」「暂停」）：填上企业应用的 AppKey 与 AppSecret，保存后在钉钉里 @ 机器人或单聊它完成绑定。",
  dingtalkKey: "AppKey（robotCode）",
  dingtalkSecret: "AppSecret",
  dingtalkSave: "保存",
  dingtalkSaved: (code: string) => `已保存：${code}`,
  dingtalkChange: "更换",
  dingtalkRemove: "删除凭据",
  dingtalkRemoveConfirmTitle: "删除这个钉钉机器人？",
  dingtalkRemoveConfirmBody: "删除后已绑定的会话和自动建出的推送渠道也会一起清掉。",
  dingtalkRemoveConfirmOk: "删除",
  dingtalkLinksEmpty: "还没有绑定钉钉会话。",
  dingtalkLinkGroup: "群",
  dingtalkLinkDm: "单聊",
  dingtalkUnbind: "解绑",
  dingtalkUnbindConfirmTitle: "解除这个会话的绑定？",
  dingtalkUnbindConfirmBody: "解除后这里不再收到提醒，钉钉里的关键字指令也会失效。",
  dingtalkUnbindConfirmOk: "解除绑定",
  dingtalkKeywords: "绑定后可在钉钉里发「帮助」看全部指令，比如：任务 / 检查 / 新建 / 额度 / 解绑",
  noChannelHint: "提醒只会发送到已配置的推送渠道。",
  noChannelLink: "去配置推送渠道",
  fields: {
    access_token: "Webhook 地址或 access_token",
    secret: "加签密钥（可留空）",
    robot_code: "AppKey（robotCode）",
    robot_secret: "AppSecret",
    conversation_id: "群会话 openConversationId",
    token: "Webhook 地址或 token",
    key_or_url: "Webhook 地址或 key",
    sendkey: "SendKey",
    device_key: "Device Key",
    server: "服务器地址（可留空）",
    smtp_host: "SMTP 服务器",
    smtp_port: "SMTP 端口（默认 587）",
    username: "邮箱账号",
    password: "邮箱密码",
    from: "发件地址",
    to: "收件地址",
    url: "Apprise URL",
  },
  platforms: {
    dingtalk_app: {
      label: "钉钉机器人",
      hint: "绑定钉钉会话后自动创建，不用手填。",
    },
    dingtalk: {
      label: "钉钉群机器人",
      hint: "在钉钉群 → 群设置 → 机器人 → 添加自定义机器人，把生成的 Webhook 地址整段粘贴即可（会自动识别 access_token）；只收提醒配这一个渠道就够了。如已开启加签，把加签密钥填到下一项。",
    },
    feishu: {
      label: "飞书机器人",
      hint: "在飞书群 → 设置 → 群机器人 → 添加自定义机器人，把 Webhook 地址整段粘贴即可（会自动识别 token）。",
    },
    wecombot: {
      label: "企业微信机器人",
      hint: "在企业微信群里添加群机器人，把 Webhook 地址整段粘贴进来。",
    },
    schan: {
      label: "Server酱",
      hint: "在 sct.ftqq.com 获取 SendKey 后粘贴。",
    },
    bark: {
      label: "Bark（iOS）",
      hint: "打开 Bark App 复制 Device Key；自建服务器时填写服务器地址。",
    },
    mailto: {
      label: "邮件",
      hint: "使用你自己的邮箱 SMTP；多数邮箱需要开启“SMTP 密码”或应用专用密码。",
    },
    custom: {
      label: "自定义（Apprise URL）",
      hint: "支持 dingtalk:// feishu:// wecombot:// schan:// bark:// pushdeer:// ntfy:// gotify:// tgram:// mailto:// json:// 等。",
    },
  },
} as const;

export const OPS = {
  syncIntro: "门店同步使用你当前登录账号的 Nike 连接；账号池、调度参数与审计日志不在本版本范围内。",
  //: 上游指向本地假服务时替换上面那句：别让离线联调看起来像真实同步。
  syncIntroOffline: "当前是离线联调环境：门店清单来自本地假服务（stub），不会访问真实 Nike。",
  offlineTag: "离线联调",
  healthRunningJobs: "进行中检查",
  healthFailedJobs: "今日失败检查",
  healthStoreCoverage: "已登记门店",
  tenantTypePlatform: "管理后台",
  tenantTypeCustomer: "客户",
} as const;

export const STORE_PAGE = {
  capability: "无法获取一家店的全部在售商品。本页只显示已监控货号的探测结果，不代表这家店的全部商品。",
  filterPlaceholder: "按货号筛选",
  currentStyle: (code: string) => `当前展示货号：${code}`,
  changesTitle: (count: number) => `本店近 14 天变化（${count} 条）`,
  resultsTitle: "本店已监控的结果",
} as const;

export const CHANGES = {
  filterNote:
    "只有结果可靠的探测才会生成记录：未探测、结果不完整和缺少尺码表的情况不会算作补货或售罄。",
  title: (count: number) => `变化流水（近 30 天，共 ${count} 条）`,
  filterAll: "全部",
} as const;

export const STYLE_PAGE = {
  filterPlaceholder: "筛选尺码，例如 42",
  showAllStores: "显示全部门店",
  inStockOnly: "仅有货门店",
  countSummary: (stores: number, sizes: number) => `共 ${stores} 家门店、${sizes} 个尺码。`,
  matrixTitle: "货号 × 门店 × 尺码",
  detailTitle: "明细（含价格与促销）",
  structureWarning: "部分门店缺少尺码表",
  matrixEmptyCell: "这家店没有这个尺码，或还没探测",
} as const;

export const SEPARATOR = "；";

/* ------------------------------------------------------------------ */
/* 格式函数（§2.6）                                                    */
/* ------------------------------------------------------------------ */

const dateTimeFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

interface ZonedParts {
  y: number;
  mo: number;
  d: number;
  h: number;
  mi: number;
  s: number;
  dayNumber: number;
}

function zonedParts(value: string | number | Date | null | undefined): ZonedParts | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = dateTimeFormatter.formatToParts(date);
  const pick = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? "0");
  const y = pick("year");
  const mo = pick("month");
  const d = pick("day");
  return {
    y,
    mo,
    d,
    h: pick("hour"),
    mi: pick("minute"),
    s: pick("second"),
    dayNumber: Math.floor(Date.UTC(y, mo - 1, d) / 86_400_000),
  };
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 绝对时间：今天 HH:mm / 昨天 HH:mm / M月D日 HH:mm / YYYY-MM-DD HH:mm。 */
export function formatDate(value: string | number | Date | null | undefined): string {
  const parts = zonedParts(value);
  if (!parts) return "—";
  const now = zonedParts(new Date()) as ZonedParts;
  const diff = now.dayNumber - parts.dayNumber;
  const hm = `${pad2(parts.h)}:${pad2(parts.mi)}`;
  if (diff === 0) return `今天 ${hm}`;
  if (diff === 1) return `昨天 ${hm}`;
  if (diff > 1 && diff <= 7) return `${parts.mo}月${parts.d}日 ${hm}`;
  return `${parts.y}-${pad2(parts.mo)}-${pad2(parts.d)} ${hm}`;
}

/** 精确到秒（页脚/详情）：YYYY-MM-DD HH:mm:ss。 */
export function formatDateTimeSeconds(value: string | number | Date | null | undefined): string {
  const parts = zonedParts(value);
  if (!parts) return "—";
  return `${parts.y}-${pad2(parts.mo)}-${pad2(parts.d)} ${pad2(parts.h)}:${pad2(parts.mi)}:${pad2(parts.s)}`;
}

/** 日期分组标题：YYYY年M月D日。 */
export function formatDayTitle(value: string | number | Date | null | undefined): string {
  const parts = zonedParts(value);
  if (!parts) return "—";
  return `${parts.y}年${parts.mo}月${parts.d}日`;
}

/** 相对时间（新鲜度）：刚刚 / N 分钟前 / N 小时前 / N 天前（>2h 追加已陈旧）。 */
export function formatRelative(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === "") return "尚未探测";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "尚未探测";
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60_000));
  const stale = minutes > 120 ? "（数据已陈旧）" : "";
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前${stale}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前${stale}`;
  const days = Math.floor(hours / 24);
  return `${days} 天前${stale}`;
}

/** 价格：¥1,234.56；有货缺价 = 待补价；非有货 = —；永不显示 ¥0。 */
export function formatPrice(
  value: number | null | undefined,
  availability?: Availability,
): string {
  if (value !== null && value !== undefined && value > 0) {
    return `¥${value.toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }
  return availability === "IN_STOCK" ? "待补价" : "—";
}

/** 价格变化：¥旧 → ¥新；缺失侧 —；两侧相同只显示新值。 */
export function formatPriceChange(
  before: number | null | undefined,
  after: number | null | undefined,
): string {
  const side = (value: number | null | undefined) =>
    value !== null && value !== undefined && value > 0
      ? `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
      : "—";
  if (before === after) return side(after);
  return `${side(before)} → ${side(after)}`;
}

/** 百分比：四舍五入整数 + %。 */
export function formatPercent(value: number): string {
  return `${Math.round(value)}%`;
}

/** 倒计时：mm:ss，超过 1 小时用 H小时mm分。 */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total >= 3600) {
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    return `${h}小时${pad2(m)}分`;
  }
  return `${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`;
}

/** 按钮冷却：N 秒后可重新获取 / 重新获取验证码。 */
export function resendLabel(seconds: number): string {
  return seconds > 0 ? `${Math.ceil(seconds)} 秒后可重新获取` : "重新获取验证码";
}

/** 周期：<1 小时 N 分钟；整小时 N 小时；<1 分钟不出现。 */
export function formatCadence(seconds: number): string {
  if (seconds < 60) return "";
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  return `${Math.round(seconds / 60)} 分钟`;
}

export const CADENCE_OPTIONS = [
  { value: 600, label: "每 10 分钟" },
  { value: 1800, label: "每 30 分钟" },
  { value: 3600, label: "每 1 小时" },
  { value: 7200, label: "每 2 小时" },
  { value: 21600, label: "每 6 小时" },
  { value: 43200, label: "每 12 小时" },
  { value: 86400, label: "每 24 小时" },
] as const;

/** 数量：整数不加单位，>9999 加千分位。 */
export function formatCount(value: number): string {
  return Math.abs(value) > 9999 ? value.toLocaleString("zh-CN") : String(value);
}

/** 额度：今日查询 X / Y 次。 */
export function formatQuota(used: number, budget: number): string {
  return `今日查询 ${formatCount(used)} / ${formatCount(budget)} 次`;
}

/** 门店：门店号 · 门店名；无名字时只写门店号。 */
export function formatStoreLabel(code: string, name?: string | null): string {
  return name ? `${code} · ${name}` : code;
}

/** 检查轮次编号：# + 前 8 位。 */
export function formatScanId(id: string): string {
  return `#${id.slice(0, 8)}`;
}

/** 列表/矩阵：接口未给 last_checked_at 时，取单元格里最晚的探测时间。 */
export function latestCheckedAt(
  fallback: string | null | undefined,
  cells: { checked_at: string | null }[],
): string | null {
  if (fallback) return fallback;
  let latest: string | null = null;
  for (const cell of cells) {
    if (cell.checked_at && (!latest || cell.checked_at > latest)) latest = cell.checked_at;
  }
  return latest;
}

/** 促销：多条用；连接；无促销 —。 */
export function formatPromotions(promotions: string[] | null | undefined): string {
  return promotions && promotions.length > 0 ? promotions.join(SEPARATOR) : "—";
}

/** 尺码：原样；空 —。 */
export function formatSizeLabel(value: string | null | undefined): string {
  return value || "—";
}

/** 检查范围：全部门店 / {n} 家门店。 */
export function formatStoreScope(allStores: boolean, count: number): string {
  return allStores ? "全部门店" : `${count} 家门店`;
}

/** 空值占位：—。 */
export function dash(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}
