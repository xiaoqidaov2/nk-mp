import { Alert, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { APP_NAME, ERR, LOGIN_SUBTITLE, loginErrorText } from "@/copy";
import { ApiError } from "@/data/client";

interface LoginForm {
  email: string;
  password: string;
}

export function LoginPage() {
  const { login, user } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [failedAttempts, setFailedAttempts] = useState(0);
  const [submitting, setSubmitting] = useState(false);

  if (user) return <Navigate to="/dashboard" replace />;

  const onSubmit = async (values: LoginForm) => {
    setSubmitting(true);
    setError("");
    try {
      await login(values.email, values.password);
      setFailedAttempts(0);
      navigate("/dashboard", { replace: true });
    } catch (err) {
      const attempts = failedAttempts + 1;
      setFailedAttempts(attempts);
      setError(err instanceof ApiError ? loginErrorText(err.message, attempts) : ERR.network);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ display: "grid", placeItems: "center", minHeight: "100vh", padding: 16 }}>
      <Card style={{ width: "min(380px, 100%)", boxShadow: "none" }}>
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          {APP_NAME}
        </Typography.Title>
        <Typography.Paragraph type="secondary">{LOGIN_SUBTITLE}</Typography.Paragraph>
        {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
        <Form<LoginForm>
          layout="vertical"
          onFinish={onSubmit}
          requiredMark={false}
          validateTrigger="onBlur"
        >
          <Form.Item
            label="邮箱"
            name="email"
            rules={[
              { required: true, message: "请输入邮箱" },
              { type: "email", message: "邮箱格式不正确" },
            ]}
          >
            <Input autoComplete="username" placeholder="you@company.com" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            {submitting ? "正在登录…" : "登录"}
          </Button>
        </Form>
      </Card>
    </div>
  );
}
