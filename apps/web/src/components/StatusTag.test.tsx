import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MatrixCellBadge, StatusLegend } from "./StatusTag";

describe("MatrixCellBadge", () => {
  it("shows stock with the price when available", () => {
    render(
      <MatrixCellBadge availability="IN_STOCK" priceStatus="OK" totalPrice={384.45} />,
    );
    expect(screen.getByText(/¥384\.45/)).toBeInTheDocument();
  });

  it("marks in-stock cells without a price as pending, not free", () => {
    render(<MatrixCellBadge availability="IN_STOCK" priceStatus="MISSING" />);
    expect(screen.getByText(/待补价/)).toBeInTheDocument();
  });

  it("distinguishes unknown from out of stock", () => {
    render(<MatrixCellBadge availability="UNKNOWN" />);
    expect(screen.getByText(/未探测/)).toBeInTheDocument();
    expect(screen.queryByText(/无货/)).not.toBeInTheDocument();
  });

  it("renders every status in the legend", () => {
    render(<StatusLegend />);
    ["有货", "无货", "不售", "未探测", "无尺码结构"].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });
});
