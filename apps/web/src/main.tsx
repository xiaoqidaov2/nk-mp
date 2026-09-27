import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { QueryProvider } from "./data/QueryProvider";
import { appTheme } from "./theme";
import "./styles/global.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("root container missing");
}

createRoot(container).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={appTheme}>
      <QueryProvider>
        <BrowserRouter>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryProvider>
    </ConfigProvider>
  </StrictMode>,
);
