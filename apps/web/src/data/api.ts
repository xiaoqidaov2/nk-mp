import { api, apiRequest } from "./client";
import type {
  ChangeEvent,
  CredentialStatus,
  CurrentUser,
  Dashboard,
  Matrix,
  MonitorTask,
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
  list: () => api.get<ScanJob[]>("/scans"),
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
  credential: () => api.get<CredentialStatus>("/ops/credentials"),
  refreshCredential: () => api.post<CredentialStatus>("/ops/credentials/refresh"),
  syncStores: () => api.post<{ ok: boolean; synced?: number }>("/ops/stores/sync"),
};

export interface ScanProgressHandlers {
  onProgress: (job: ScanJob) => void;
  onDone: () => void;
  onError: (message: string) => void;
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
    if (!closed) startPolling();
  };

  return () => {
    closed = true;
    stopPolling();
    source.close();
  };
}
