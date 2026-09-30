import type { ThemeConfig } from "antd";

/** 中性商务后台视觉系统。单一来源：所有颜色/字号只在此处与 global.css 的
 *  CSS 变量中定义；业务代码禁止出现颜色字面量。 */
export const appTheme: ThemeConfig = {
  token: {
    // —— 字体 ——
    fontFamily:
      '"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    fontSize: 14,
    fontSizeSM: 13,
    fontSizeLG: 16,
    fontSizeXL: 20,
    fontSizeHeading1: 24,
    fontSizeHeading2: 20,
    fontSizeHeading3: 16,
    fontSizeHeading4: 14,
    fontSizeHeading5: 14,
    lineHeight: 1.5714, // 14px → 22px
    lineHeightSM: 1.5385, // 13px → 20px
    lineHeightLG: 1.5, // 16px → 24px
    lineHeightHeading1: 1.3334, // 24 → 32
    lineHeightHeading2: 1.4, // 20 → 28
    lineHeightHeading3: 1.5, // 16 → 24
    lineHeightHeading4: 1.5714,
    lineHeightHeading5: 1.5714,
    fontWeightStrong: 600,

    // —— 语义色 ——
    colorPrimary: "#2f6fed",
    colorPrimaryHover: "#1d4ed8",
    colorPrimaryActive: "#1b3fb8",
    colorPrimaryBg: "#e8f0fe",
    colorPrimaryBgHover: "#dbe7fd",
    colorPrimaryBorder: "#bcd0fa",
    colorLink: "#1d4ed8",
    colorLinkHover: "#2f6fed",
    colorLinkActive: "#1b3fb8",
    colorSuccess: "#157347",
    colorSuccessBg: "#e7f6ee",
    colorSuccessBorder: "#b7e0cc",
    colorWarning: "#8a5a08",
    colorWarningBg: "#fef3c7",
    colorWarningBorder: "#ecd9a0",
    colorError: "#c0392b",
    colorErrorBg: "#fdecea",
    colorErrorBorder: "#f3c0ba",
    colorInfo: "#2f6fed",
    colorInfoBg: "#e8f0fe",
    colorInfoBorder: "#bcd0fa",

    // —— 中性 / 文本 ——
    colorTextBase: "#1f2430",
    colorText: "#1f2430",
    colorTextSecondary: "#4b5563",
    colorTextTertiary: "#6b7280", // 仅白底或非文本
    colorTextQuaternary: "#9aa2b1", // 仅非文本/禁用
    colorTextDisabled: "#a8b0bd",
    colorBgBase: "#ffffff",
    colorBgContainer: "#ffffff",
    colorBgElevated: "#ffffff",
    colorBgLayout: "#f4f6f9",
    colorBorder: "#d5dae2",
    colorBorderSecondary: "#e6e9ef",
    colorSplit: "#eceef3",
    colorFillSecondary: "#f1f2f5",
    colorFillTertiary: "#f5f6f8",
    colorFillQuaternary: "#f9fafb",

    // —— 形状 ——
    borderRadius: 4,
    borderRadiusSM: 4,
    borderRadiusLG: 6,
    borderRadiusXS: 2,
    lineWidth: 1,
    lineWidthBold: 2,

    // —— 控件尺寸 ——
    controlHeight: 32,
    controlHeightSM: 28,
    controlHeightLG: 40,
    controlHeightXS: 24,

    // —— 间距 ——
    padding: 16,
    paddingXS: 8,
    paddingSM: 12,
    paddingMD: 20,
    paddingLG: 24,
    paddingXL: 32,
    margin: 16,
    marginXS: 8,
    marginSM: 12,
    marginMD: 20,
    marginLG: 24,
    marginXL: 32,

    // —— 阴影 ——
    boxShadow: "0 4px 12px rgba(16,24,40,0.08)",
    boxShadowSecondary: "0 4px 12px rgba(16,24,40,0.08)",
    boxShadowTertiary: "0 1px 2px rgba(16,24,40,0.06)",

    // —— 动效 ——
    motionDurationFast: "0.1s",
    motionDurationMid: "0.2s",
    motionDurationSlow: "0.3s",
    wireframe: false,
  },

  components: {
    Layout: {
      bodyBg: "#f4f6f9",
      headerBg: "#ffffff",
      headerHeight: 56,
      headerPadding: "0 16px",
      headerColor: "#1f2430",
      siderBg: "#ffffff",
      lightSiderBg: "#ffffff",
      footerBg: "#f4f6f9",
      footerPadding: "12px 16px 20px",
      triggerBg: "#ffffff",
      triggerColor: "#4b5563",
    },

    Menu: {
      itemBg: "transparent",
      itemColor: "#4b5563",
      itemHoverBg: "#f2f6ff",
      itemHoverColor: "#1d4ed8",
      itemSelectedBg: "#e8f0fe",
      itemSelectedColor: "#1d4ed8",
      itemBorderRadius: 4,
      itemHeight: 40,
      itemMarginInline: 8,
      iconSize: 16,
      collapsedIconSize: 16,
      groupTitleColor: "#6b7280",
      dangerItemColor: "#c0392b",
    },

    Table: {
      headerBg: "#f7f8fa",
      headerColor: "#4b5563",
      headerSplitColor: "transparent",
      headerBorderRadius: 6,
      borderColor: "#eceef3",
      rowHoverBg: "#f2f6ff",
      rowSelectedBg: "#e8f0fe",
      rowSelectedHoverBg: "#dbe7fd",
      footerBg: "#f7f8fa",
      footerColor: "#4b5563",
      cellPaddingBlock: 10, // 默认/middle ≈ 42px 行高
      cellPaddingInline: 14,
      cellPaddingBlockMD: 10,
      cellPaddingInlineMD: 14,
      cellPaddingBlockSM: 8, // size="small"（本项目全站默认）→ 38px 行高
      cellPaddingInlineSM: 12,
      cellFontSize: 14,
      cellFontSizeMD: 14,
      cellFontSizeSM: 14,
    },

    Card: {
      headerBg: "#ffffff",
      headerFontSize: 16,
      headerFontSizeSM: 14,
      headerHeight: 48,
      headerHeightSM: 40,
      headerPadding: 16,
      headerPaddingSM: 12,
      bodyPadding: 16,
      bodyPaddingSM: 12,
      paddingLG: 16,
      extraColor: "#1d4ed8",
    },

    Button: {
      fontWeight: 400,
      contentFontSize: 14,
      contentFontSizeSM: 13,
      contentFontSizeLG: 14,
      paddingInline: 16,
      paddingInlineSM: 12,
      paddingInlineLG: 20,
      defaultBorderColor: "#d5dae2",
      defaultShadow: "none",
      primaryShadow: "none",
      dangerShadow: "none",
      textHoverBg: "#f2f4f7",
      controlHeight: 32,
      controlHeightSM: 28,
      controlHeightLG: 40,
    },

    Tag: {
      defaultBg: "#f1f2f5",
      defaultColor: "#4b5563",
      fontSizeSM: 12,
      lineHeightSM: 1.6667,
      borderRadiusSM: 4,
    },

    Alert: {
      defaultPadding: "10px 12px",
      withDescriptionPadding: "14px 16px",
      withDescriptionIconSize: 16,
    },

    Steps: {
      iconSize: 28,
      iconFontSize: 14,
      titleLineHeight: 22,
      descriptionMaxWidth: 180,
      navArrowColor: "#c3c9d4",
    },

    Descriptions: {
      labelBg: "#f7f8fa",
      titleColor: "#1f2430",
      labelColor: "#4b5563",
      contentColor: "#1f2430",
      extraColor: "#1d4ed8",
      itemPaddingBottom: 12,
      colonMarginRight: 8,
      colonMarginLeft: 4,
    },

    Form: {
      labelColor: "#1f2430",
      labelFontSize: 14,
      labelRequiredMarkColor: "#c0392b",
      labelColonMarginInlineStart: 4,
      labelColonMarginInlineEnd: 8,
      itemMarginBottom: 16,
      verticalLabelPadding: "0 0 4px",
    },

    Input: {
      paddingBlock: 5,
      paddingInline: 12,
      paddingBlockSM: 3,
      paddingInlineSM: 10,
      paddingBlockLG: 7,
      paddingInlineLG: 14,
      addonBg: "#f7f8fa",
      hoverBorderColor: "#2f6fed",
      activeBorderColor: "#2f6fed",
      activeShadow: "0 0 0 2px rgba(47,111,237,0.15)",
      errorActiveShadow: "0 0 0 2px rgba(192,57,43,0.15)",
      warningActiveShadow: "0 0 0 2px rgba(138,90,8,0.15)",
    },

    Select: {
      selectorBg: "#ffffff",
      optionActiveBg: "#f2f4f7",
      optionSelectedBg: "#e8f0fe",
      optionSelectedColor: "#1d4ed8",
      optionSelectedFontWeight: 600,
      optionPadding: "6px 12px",
      optionFontSize: 14,
      optionLineHeight: 1.5714,
      optionHeight: 32,
    },

    Segmented: {
      trackBg: "#eef0f3",
      trackPadding: 2,
      itemColor: "#4b5563",
      itemHoverColor: "#1f2430",
      itemHoverBg: "rgba(255,255,255,0.6)",
      itemSelectedBg: "#ffffff",
      itemSelectedColor: "#1d4ed8",
      itemActiveBg: "#ffffff",
    },

    Progress: {
      defaultColor: "#2f6fed",
      remainingColor: "#eceef3",
      lineBorderRadius: 4,
      circleTextColor: "#1f2430",
      circleTextFontSize: "14px",
    },

    // antd 5 的 Empty 无组件级 token，视觉全部由 global.css + EmptyState 组件承担。
    Empty: {},

    Result: {
      titleFontSize: 20,
      subtitleFontSize: 14,
      iconFontSize: 56,
      extraMargin: "20px 0 0 0",
    },

    Drawer: {
      zIndexPopup: 1000,
      footerPaddingBlock: 12,
      footerPaddingInline: 16,
    },

    Tooltip: {
      zIndexPopup: 1070,
      colorBgSpotlight: "#1f2430",
      colorTextLightSolid: "#ffffff",
    },
  },
};
