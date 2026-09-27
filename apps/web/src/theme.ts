import type { ThemeConfig } from "antd";

/** Neutral business-admin palette; deliberately avoids any brand styling. */
export const appTheme: ThemeConfig = {
  token: {
    colorPrimary: "#2f6fed",
    colorSuccess: "#1f8a5f",
    colorWarning: "#c98a12",
    colorError: "#c0392b",
    colorInfo: "#2f6fed",
    colorBgLayout: "#f4f6f9",
    colorBorderSecondary: "#e6e9ef",
    borderRadius: 4,
    fontSize: 13,
    controlHeight: 32,
  },
  components: {
    Layout: { headerBg: "#ffffff", siderBg: "#ffffff", bodyBg: "#f4f6f9" },
    Table: {
      headerBg: "#f7f8fa",
      rowHoverBg: "#f2f6ff",
      cellPaddingBlock: 6,
      cellPaddingInline: 10,
      headerSplitColor: "transparent",
    },
    Menu: { itemSelectedBg: "#eaf1ff", itemSelectedColor: "#2f6fed" },
    Card: { paddingLG: 16 },
  },
};
