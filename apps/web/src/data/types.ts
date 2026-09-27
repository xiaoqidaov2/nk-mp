export type Availability =
  | "IN_STOCK"
  | "OUT_OF_STOCK"
  | "NOT_CARRIED"
  | "UNKNOWN"
  | "STRUCTURE_MISSING";

export type PriceStatus = "OK" | "MISSING";
export type Trust = "TRUSTED" | "UNTRUSTED";

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  role: "ops_admin" | "ops_operator" | "tenant_admin" | "tenant_viewer";
  tenant_id: string;
  tenant_name: string;
  tenant_slug: string;
  is_platform: boolean;
}

export interface StoreOut {
  store_code: string;
  display_name: string;
  address: string;
  concept: string;
  is_active: boolean;
}

export interface StyleOut {
  style_code: string;
  title: string;
  image_url: string;
}

export interface MonitorTask {
  id: string;
  name: string;
  store_codes: string[];
  style_codes: string[];
  cadence_seconds: number;
  is_active: boolean;
  notify_on_restock: boolean;
  notify_on_sold_out: boolean;
  notify_on_price_change: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string;
  store_count: number;
  style_count: number;
}

export interface ScanJob {
  id: string;
  kind: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED";
  store_codes: string[];
  style_codes: string[];
  total_stores: number;
  done_stores: number;
  failed_stores: number;
  unknown_stores: number;
  attempt: number;
  error_summary: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  progress: number;
}

export interface ScanStore {
  store_code: string;
  store_name: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "UNKNOWN";
  attempt: number;
  error_code: string | null;
  message: string;
  observations_written: number;
  events_written: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface MatrixCell {
  store_code: string;
  store_name: string;
  size_label: string;
  availability: Availability;
  trust: Trust;
  price_status: PriceStatus;
  price: number | null;
  total_price: number | null;
  promotions: string[];
  error_code: string | null;
  checked_at: string | null;
}

export interface Matrix {
  style_code: string;
  title: string;
  image_url: string;
  sizes: string[];
  stores: { store_code: string; store_name: string }[];
  cells: MatrixCell[];
  structure_status: string;
  last_checked_at: string | null;
}

export interface ChangeEvent {
  id: number;
  event_type: "RESTOCK" | "SOLD_OUT" | "PRICE_CHANGE" | "NEW_ARRIVAL_AT_STORE" | "SCAN_FAILED";
  store_code: string;
  store_name: string;
  style_code: string;
  style_title: string;
  size_label: string;
  before_status: string | null;
  after_status: string | null;
  before_total_price: number | null;
  after_total_price: number | null;
  promotions: string[];
  checked_at: string;
  detected_at: string;
}

export interface Dashboard {
  active_tasks: number;
  monitored_styles: number;
  monitored_stores: number;
  store_coverage: number;
  restock_today: number;
  sold_out_today: number;
  price_change_today: number;
  new_arrival_today: number;
  failed_jobs: number;
  running_jobs: number;
  last_checked_at: string | null;
  credential_status: string;
  credential_expires_at: string | null;
  review_requests_today: number;
  daily_review_budget: number;
}

export interface CredentialStatus {
  account_key: string;
  status: "ACTIVE" | "EXPIRING" | "NEEDS_AUTHORIZATION";
  access_expires_at: string | null;
  last_refreshed_at: string | null;
  last_error: string;
  has_refresh_token: boolean;
}
