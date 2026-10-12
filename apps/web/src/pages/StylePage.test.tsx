import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { matrixApi, taskApi } from "@/data/api";
import type { Matrix, MatrixCell } from "@/data/types";

import { StylePage } from "./StylePage";

vi.mock("@/data/api", () => ({
  matrixApi: { get: vi.fn() },
  taskApi: { list: vi.fn() },
  scanApi: { create: vi.fn() },
}));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ user: { id: "user-a" }, canWrite: true, isOps: false, loading: false }),
}));

vi.mock("@/components/NikeCredentialCard", () => ({
  NikeConnectionNotice: () => null,
}));

const matrixMock = vi.mocked(matrixApi.get);
const taskListMock = vi.mocked(taskApi.list);

function cell(storeCode: string, size: string, availability: MatrixCell["availability"]): MatrixCell {
  return {
    store_code: storeCode,
    store_name: "",
    size_label: size,
    availability,
    trust: "TRUSTED",
    price_status: "MISSING",
    price: null,
    total_price: null,
    promotions: [],
    error_code: null,
    checked_at: "2026-10-06T12:00:00+00:00",
  };
}

function matrix(cells: MatrixCell[]): Matrix {
  return {
    style_code: "CD7069-003",
    title: "CD7069-003",
    image_url: "",
    sizes: ["40", "41", "42"],
    stores: [
      { store_code: "S-A", store_name: "" },
      { store_code: "S-B", store_name: "" },
    ],
    cells,
    structure_status: "OK",
    last_checked_at: "2026-10-06T12:00:00+00:00",
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/styles/CD7069-003"]}>
        <AntdApp>
          <Routes>
            <Route path="/styles/:styleCode" element={<StylePage />} />
          </Routes>
        </AntdApp>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StylePage", () => {
  beforeEach(() => {
    matrixMock.mockReset();
    taskListMock.mockReset();
    taskListMock.mockResolvedValue([]);
  });

  it("整货号无货时给数据型空态，不把默认视图说成筛选无结果", async () => {
    matrixMock.mockResolvedValue(
      matrix([
        cell("S-A", "40", "NOT_CARRIED"),
        cell("S-A", "41", "NOT_CARRIED"),
        cell("S-B", "40", "OUT_OF_STOCK"),
        cell("S-B", "41", "OUT_OF_STOCK"),
      ]),
    );
    renderPage();
    expect(
      await screen.findByText("该货号在已监控的 2 家门店暂无货源（未铺货或已售罄）。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("当前筛选无结果（清除筛选）")).not.toBeInTheDocument();
  });

  it("有货优先：在货尺码多的门店排在前面", async () => {
    matrixMock.mockResolvedValue(
      matrix([
        cell("S-A", "40", "IN_STOCK"),
        cell("S-B", "40", "IN_STOCK"),
        cell("S-B", "41", "IN_STOCK"),
      ]),
    );
    const { container } = renderPage();
    await screen.findAllByText("S-B");
    const text = container.textContent ?? "";
    expect(text.indexOf("S-B")).toBeGreaterThanOrEqual(0);
    expect(text.indexOf("S-B")).toBeLessThan(text.indexOf("S-A"));
  });

  it("尺码筛选没有命中时才提示筛选无结果", async () => {
    matrixMock.mockResolvedValue(matrix([cell("S-A", "40", "IN_STOCK")]));
    renderPage();
    await screen.findAllByText("S-A");
    fireEvent.change(screen.getByPlaceholderText("筛选尺码，例如 42"), {
      target: { value: "99" },
    });
    expect(await screen.findByText("没有符合「99」的尺码。")).toBeInTheDocument();
  });
});
