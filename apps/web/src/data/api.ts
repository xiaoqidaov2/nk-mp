import { api, apiRequest } from "./client";
import type {
  ChangeEvent,
  CurrentUser,
  Dashboard,
  Matrix,
  MonitorTask,
  NikeAuthAttemptOut,
  NikeBootstrapOut,
  NikeCredentialOut,
  NotificationChannel,
  NotificationDelivery,
  NotificationPlatform,
  ScanJob,
  ScanStore,
  StoreOut,
  StyleOut,
} from "./types";

export interface LoginResult {
  user: CurrentUser;
  csrf_token: string;
}

export const authApi = {
  login: (email: string, password: string) =>
    api.post<LoginResult>("/auth/login", { email, password }),
  logout: () => api.post<{ ok: boolean }>("/auth/logout"),
  me: () => api.get<CurrentUser>("/auth/me"),
};

export const catalogApi = {
  stores: (search = "") =>
    api.get<StoreOut[]>(`/stores${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  styles: (search = "") =>
    api.get<StyleOut[]>(`/styles${search ? `?search=${encodeURIComponent(search)}` : ""}`),
};

export const taskApi = {
  list: () => api.get<MonitorTask[]>("/tasks"),
  create: (payload: Partial<MonitorTask> & { name: string }) =>
    api.post<MonitorTask>("/tasks", payload),
  update: (id: string, payload: Partial<MonitorTask>) =>
    api.patch<MonitorTask>(`/tasks/${id}`, payload),
  remove: (id: string) => api.delete<void>(`/tasks/${id}`),
};

export const scanApi = {
  list: (taskId?: string) =>
    api.get<ScanJob[]>(`/scans${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),
  create: (payload: {
    store_codes?: string[];
    style_codes?: string[];
    all_stores?: boolean;
    task_id?: string;
  }) => api.post<ScanJob>("/scans", payload),
  get: (id: string) => api.get<ScanJob>(`/scans/${id}`),
  stores: (id: string) => api.get<ScanStore[]>(`/scans/${id}/stores`),
  retryFailed: (id: string) => api.post<ScanJob>(`/scans/${id}/retry-failed`),
};

export const matrixApi = {
  get: (styleCode: string, storeCodes: string[] = []) => {
    const query = new URLSearchParams({ style_code: styleCode });
    if (storeCodes.length) query.set("store_codes", storeCodes.join(","));
    return api.get<Matrix>(`/matrix?${query.toString()}`);
  },
};

export const changeApi = {
  list: (params: { event_type?: string; days?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.event_type) query.set("event_type", params.event_type);
    if (params.days) query.set("days", String(params.days));
    const suffix = query.toString();
    return api.get<ChangeEvent[]>(`/changes${suffix ? `?${suffix}` : ""}`);
  },
};

export const dashboardApi = {
  get: () => api.get<Dashboard>("/dashboard"),
};

export const opsApi = {
  syncStores: () => api.post<{ ok: boolean; synced?: number }>("/ops/stores/sync"),
  /** 运维自检：当前进程实际使用的 Nike 上游（离线联调时是本地假服务）。 */
  healthDetail: () =>
    api.get<{ queue_depth: number; running_jobs: number; stores: number; styles: number; nike_api_base: string }>(
      "/ops/health/detail",
    ),
};

/**
 * Self-service Nike binding. Routes are the本站 `me/*` contract; the owner and
 * attempts are always derived from the登录 session server-side.
 */
export const nikeCredentialApi = {
  status: () => api.get<NikeCredentialOut>("/me/nike-credential"),
  start: (payload: { phone: string; consent: true; request_id: string }) =>
    api.post<NikeAuthAttemptOut>("/me/nike-auth/sessions", payload),
  attempt: (id: string) => api.get<NikeAuthAttemptOut>(`/me/nike-auth/sessions/${id}`),
  verify: (id: string, payload: { code: string }) =>
    api.post<NikeCredentialOut>(`/me/nike-auth/sessions/${id}/verify`, payload),
  cancel: (id: string) => api.delete<void>(`/me/nike-auth/sessions/${id}`),
  disconnect: () => api.delete<void>("/me/nike-credential"),
  /** 绑定后预加载进度（门店目录按用户缓存）。 */
  bootstrap: () => api.get<NikeBootstrapOut>("/me/bootstrap"),
  /** 重新排队预加载（失败重试 / 手动刷新缓存）。 */
  startBootstrap: () => api.post<NikeBootstrapOut>("/me/bootstrap"),
};

export interface NotificationChannelPayload {
  platform: NotificationPlatform;
  name?: string;
  fields: Record<string, string>;
}

/** 推送渠道只属于当前登录用户；目标地址由服务端拼装并加密保存。 */
export const notificationApi = {
  channels: () => api.get<NotificationChannel[]>("/me/notification-channels"),
  createChannel: (payload: NotificationChannelPayload) =>
    api.post<NotificationChannel>("/me/notification-channels", payload),
  updateChannel: (id: string, payload: { name?: string; enabled?: boolean }) =>
    api.patch<NotificationChannel>(`/me/notification-channels/${id}`, payload),
  removeChannel: (id: string) => api.delete<void>(`/me/notification-channels/${id}`),
  testChannel: (id: string) =>
    api.post<{ ok: boolean; error: string | null }>(`/me/notification-channels/${id}/test`),
  deliveries: (limit = 10) =>
    api.get<NotificationDelivery[]>(`/me/notifications?limit=${limit}`),
  retryDelivery: (id: number) => api.post<{ ok: boolean }>(`/me/notifications/${id}/retry`),
};

export interface ScanProgressHandlers {
  onProgress: (job: ScanJob) => void;
  onDone: () => void;
  onError: (message: string) => void;
  /** 自动刷新断开、已回退为轮询（用于“进度自动更新已中断”提示）。 */
  onFallback?: () => void;
}

/** Subscribes to scan progress over SSE, falling back to polling on failure. */
export function subscribeScanProgress(
  jobId: string,
  handlers: ScanProgressHandlers,
): () => void {
  let closed = false;
  let pollTimer: number | undefined;
  const source = new EventSource(`/api/v1/scans/${jobId}/events`);

  const stopPolling = () => {
    if (pollTimer !== undefined) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
    }
  };

  const startPolling = () => {
    if (pollTimer !== undefined) return;
    pollTimer = window.setInterval(async () => {
      try {
        const job = await apiRequest<ScanJob>(`/scans/${jobId}`);
        handlers.onProgress(job);
        if (job.status !== "QUEUED" && job.status !== "RUNNING") {
          stopPolling();
          handlers.onDone();
        }
      } catch (error) {
        handlers.onError(error instanceof Error ? error.message : "进度轮询失败");
      }
    }, 3000);
  };

  source.addEventListener("progress", (event) => {
    try {
      handlers.onProgress(JSON.parse((event as MessageEvent).data) as ScanJob);
    } catch {
      handlers.onError("进度数据解析失败");
    }
  });
  source.addEventListener("done", () => {
    stopPolling();
    source.close();
    if (!closed) handlers.onDone();
  });
  source.onerror = () => {
    source.close();
    if (!closed) {
      startPolling();
      handlers.onFallback?.();
    }
  };

  return () => {
    closed = true;
    stopPolling();
    source.close();
  };
}
