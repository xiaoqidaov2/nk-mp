import { api } from "./client";

export interface TenantRow {
  id: string;
  slug: string;
  name: string;
  is_platform: boolean;
  is_active: boolean;
  daily_review_budget: number;
  task_count: number;
}

export function opApiList(): Promise<TenantRow[]> {
  return api.get<TenantRow[]>("/ops/tenants");
}
