import {
  AppstoreOutlined,
  CloseOutlined,
  DashboardOutlined,
  HistoryOutlined,
  MenuOutlined,
  ToolOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Button, Drawer, Layout, Menu, Tag, Typography } from "antd";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { APP_NAME, BTN, FOOTER_NOTE, NAV_ITEMS, UNOFFICIAL_TAG } from "@/copy";

const { Header, Sider, Content, Footer } = Layout;

const MENU_ICONS: Record<string, ReactNode> = {
  "/dashboard": <DashboardOutlined />,
  "/tasks": <AppstoreOutlined />,
  "/changes": <HistoryOutlined />,
  "/account": <UserOutlined />,
  "/ops": <ToolOutlined />,
};

/** 子页面（/tasks/:id、/scans/:id…）统一高亮“监控任务”。 */
function menuKeyFor(pathname: string): string | undefined {
  if (["/tasks", "/scans", "/stores", "/styles"].some((prefix) => pathname.startsWith(prefix))) {
    return "/tasks";
  }
  return NAV_ITEMS.map((item) => item.key)
    .filter((key) => pathname.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];
}

export function AppLayout() {
  const { user, logout, isOps } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [dataOpen, setDataOpen] = useState(false);

  const visibleItems = useMemo(
    () => NAV_ITEMS.filter((item) => item.key !== "/ops" || isOps),
    [isOps],
  );

  const menuItems = useMemo(
    () =>
      visibleItems.map((item) => ({
        key: item.key,
        icon: MENU_ICONS[item.key],
        label: item.label,
      })),
    [visibleItems],
  );

  const drawerItems = useMemo(() => {
    if (!isOps) return menuItems;
    const opsIndex = menuItems.findIndex((item) => item.key === "/ops");
    if (opsIndex <= 0) return menuItems;
    return [
      ...menuItems.slice(0, opsIndex),
      { type: "divider" as const, key: "divider-ops" },
      ...menuItems.slice(opsIndex),
    ];
  }, [isOps, menuItems]);

  const selected = menuKeyFor(pathname);
  const title = NAV_ITEMS.find((item) => item.key === selected)?.label ?? APP_NAME;

  // 路由变化自动关闭抽屉（含点击遮罩/Esc/菜单项后的兜底）。
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  const accountLine = user ? `${user.tenant_name} · ${user.display_name || user.email}` : "";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={208} theme="light" className="app-sider desktop-only">
        <div className="app-sider-brand">{APP_NAME}</div>
        <Menu
          mode="inline"
          selectedKeys={selected ? [selected] : []}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: "none" }}
        />
      </Sider>

      <Layout>
        <Header className="app-header">
          <div className="app-header__left">
            <Button
              type="text"
              icon={<MenuOutlined />}
              className="mobile-only touch-target"
              aria-label="打开导航"
              aria-expanded={navOpen}
              onClick={() => setNavOpen(true)}
            />
            <span className="app-header__title">{title}</span>
            <Tag color="default">{UNOFFICIAL_TAG}</Tag>
          </div>
          <div className="app-header__right desktop-only">
            {user ? (
              <Typography.Text type="secondary">{accountLine}</Typography.Text>
            ) : null}
            <Button onClick={() => void logout()}>{BTN.logout}</Button>
          </div>
        </Header>

        <Content className="app-content">
          <Outlet />
        </Content>

        <Footer className="app-footer">
          <span className="desktop-only">{FOOTER_NOTE}</span>
          <Button
            type="link"
            size="small"
            className="mobile-only"
            style={{ padding: 0 }}
            onClick={() => setDataOpen(true)}
          >
            数据说明
          </Button>
        </Footer>
      </Layout>

      <Drawer
        placement="left"
        width={272}
        open={navOpen}
        onClose={() => setNavOpen(false)}
        title={APP_NAME}
        closable
        closeIcon={<CloseOutlined />}
        styles={{ body: { padding: 0 } }}
      >
        <Menu
          mode="inline"
          className="drawer-menu"
          selectedKeys={selected ? [selected] : []}
          items={drawerItems}
          onClick={({ key }) => {
            setNavOpen(false);
            navigate(key);
          }}
          style={{ borderInlineEnd: "none" }}
        />
        <div className="drawer-account">
          <Typography.Text type="secondary">{accountLine}</Typography.Text>
          <Button block onClick={() => void logout()}>
            {BTN.logout}
          </Button>
        </div>
      </Drawer>

      <Drawer
        placement="bottom"
        height="auto"
        open={dataOpen}
        onClose={() => setDataOpen(false)}
        title="数据说明"
      >
        <Typography.Paragraph style={{ marginBottom: 0 }}>{FOOTER_NOTE}</Typography.Paragraph>
      </Drawer>
    </Layout>
  );
}
