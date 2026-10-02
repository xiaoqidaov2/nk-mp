import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { authApi } from "@/data/api";
import { setCsrfToken, setUnauthorizedHandler } from "@/data/client";
import { clearAllNikeAttemptIds } from "@/data/nikeAttempt";
import type { CurrentUser } from "@/data/types";

interface AuthState {
  user: CurrentUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  canWrite: boolean;
  isOps: boolean;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    authApi
      .me()
      .then((me) => {
        if (active) setUser(me);
      })
      .catch(() => {
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    // 别的标签页退出登录或会话过期后，任意一个 401 都会把这里拉回登录页，
    // 而不是留着一个点什么都失败的假登录界面。
    setUnauthorizedHandler(() => {
      setCsrfToken("");
      queryClient.clear();
      setUser(null);
    });
    return () => setUnauthorizedHandler(null);
  }, [queryClient]);

  const login = useCallback(
    async (email: string, password: string) => {
      const result = await authApi.login(email, password);
      setCsrfToken(result.csrf_token);
      // Never show another account's cached responses after a switch.
      queryClient.clear();
      setUser(result.user);
    },
    [queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      setCsrfToken("");
      // Drop the in-flight SMS attempt ids and cached results; the bound
      // credential itself lives server-side and is not destroyed here.
      clearAllNikeAttemptIds();
      queryClient.clear();
      setUser(null);
    }
  }, [queryClient]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      login,
      logout,
      canWrite:
        user !== null &&
        ["ops_admin", "ops_operator", "tenant_admin"].includes(user.role),
      isOps: user !== null && ["ops_admin", "ops_operator"].includes(user.role),
    }),
    [user, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
