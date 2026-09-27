import { Spin } from "antd";
import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth/AuthContext";
import { AppLayout } from "./layout/AppLayout";

const AccountPage = lazy(() =>
  import("./pages/AccountPage").then((m) => ({ default: m.AccountPage })),
);
const ChangesPage = lazy(() =>
  import("./pages/ChangesPage").then((m) => ({ default: m.ChangesPage })),
);
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const LoginPage = lazy(() => import("./pages/LoginPage").then((m) => ({ default: m.LoginPage })));
const OpsPage = lazy(() => import("./pages/OpsPage").then((m) => ({ default: m.OpsPage })));
const ScanDetailPage = lazy(() =>
  import("./pages/ScanDetailPage").then((m) => ({ default: m.ScanDetailPage })),
);
const StorePage = lazy(() => import("./pages/StorePage").then((m) => ({ default: m.StorePage })));
const StylePage = lazy(() => import("./pages/StylePage").then((m) => ({ default: m.StylePage })));
const TaskCreatePage = lazy(() =>
  import("./pages/TaskCreatePage").then((m) => ({ default: m.TaskCreatePage })),
);
const TaskDetailPage = lazy(() =>
  import("./pages/TaskDetailPage").then((m) => ({ default: m.TaskDetailPage })),
);
const TasksPage = lazy(() => import("./pages/TasksPage").then((m) => ({ default: m.TasksPage })));

function PageFallback() {
  return (
    <div style={{ display: "grid", placeItems: "center", padding: 48 }}>
      <Spin tip="加载中" />
    </div>
  );
}

export default function App() {
  const { user, loading, isOps } = useAuth();

  if (loading) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100vh" }}>
        <Spin size="large" tip="正在校验会话" />
      </div>
    );
  }

  if (!user) {
    return (
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </Suspense>
    );
  }

  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route path="/login" element={<Navigate to="/dashboard" replace />} />
        <Route element={<AppLayout />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/tasks/new" element={<TaskCreatePage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/scans/:jobId" element={<ScanDetailPage />} />
          <Route path="/stores/:storeCode" element={<StorePage />} />
          <Route path="/styles/:styleCode" element={<StylePage />} />
          <Route path="/changes" element={<ChangesPage />} />
          <Route path="/account" element={<AccountPage />} />
          {isOps ? <Route path="/ops" element={<OpsPage />} /> : null}
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
