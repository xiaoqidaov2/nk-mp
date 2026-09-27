import {
  AppstoreOutlined,
  DashboardOutlined,
  HistoryOutlined,
  TagsOutlined,
  ToolOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Button, Layout, Menu, Space, Tag, Typography } from "antd";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";

const { Header, Sider, Content } = Layout;

export function AppLayout() {
  const { user, logout, isOps } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const items = [
    { key: "/dashboard", icon: <DashboardOutlined />, label: "概览大盘" },
    { key: "/tasks", icon: <AppstoreOutlined />, label: "监控任务" },
    { key: "/changes", icon: <HistoryOutlined />, label: "变化流水" },
    { key: "/account", icon: <UserOutlined />, label: "账户与配额" },
    ...(isOps ? [{ key: "/ops", icon: <ToolOutlined />, label: "服务方后台" }] : []),
  ];

  const selected = items
    .map((item) => item.key)
    .filter((key) => location.pathname.startsWith(key))
    .sort((a, b) => b.length - a.length)[0];

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={208} theme="light" style={{ borderRight: "1px solid #e6e9ef" }}>
        <div style={{ padding: "18px 16px", fontSize: 15, fontWeight: 600 }}>门店监控台</div>
        <Menu
          mode="inline"
          selectedKeys={selected ? [selected] : []}
          items={items}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: "none" }}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            borderBottom: "1px solid #e6e9ef",
            paddingInline: 20,
          }}
        >
          <Space size={12}>
            <Typography.Text strong style={{ fontSize: 15 }}>
              Nike 门店库存与价格监控
            </Typography.Text>
            <Tag color="default">非官方，仅信息聚合</Tag>
          </Space>
          <Space size={12}>
            {user ? (
              <Space size={8}>
                <TagsOutlined />
                <Typography.Text>
                  {user.tenant_name}
                  <Typography.Text type="secondary"> · {user.display_name || user.email}</Typography.Text>
                </Typography.Text>
              </Space>
            ) : null}
            <Button size="small" onClick={() => void logout()}>
              退出登录
            </Button>
          </Space>
        </Header>
        <Content style={{ padding: 16 }}>
          <Outlet />
        </Content>
        <footer
          style={{
            padding: "12px 20px 20px",
            fontSize: 12,
            color: "#6b7280",
          }}
        >
          数据来自公开渠道聚合，与品牌方无关联；单店全量商品清单不可得，页面仅展示已监控货号与满减专区预览池。
        </footer>
      </Layout>
    </Layout>
  );
}

