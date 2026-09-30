import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { nikeCredentialApi } from "@/data/api";
import type { NikeBootstrapOut } from "@/data/types";

/** 绑定预加载进度：进行中自动轮询，结束即停。 */
export function useBootstrapQuery(enabled = true) {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  return useQuery<NikeBootstrapOut>({
    queryKey: ["nike-bootstrap", userId],
    queryFn: () => nikeCredentialApi.bootstrap(),
    enabled: Boolean(userId) && enabled,
    retry: false,
    staleTime: 0,
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === "PENDING" || state === "RUNNING" ? 800 : false;
    },
  });
}
