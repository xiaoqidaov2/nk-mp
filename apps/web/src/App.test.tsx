import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const authState = vi.hoisted(() => ({ user: null as null | { id: string }, isOps: false }));

vi.mock("./auth/AuthContext", () => ({
  useAuth: () => ({ ...authState, loading: false, canWrite: true }),
}));

vi.mock("./pages/LoginPage", () => ({
  LoginPage: () => <div>登录页</div>,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="here">{`${location.pathname}${location.search}`}</div>;
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("deep links through login", () => {
  beforeEach(() => {
    authState.user = null;
    authState.isOps = false;
  });

  it("keeps the pushed task path in the login redirect", () => {
    renderAt("/tasks/1de593d5-b698-4be3-b4e2-ce5ed3031e65");
    expect(screen.getByTestId("here").textContent).toBe(
      "/login?next=%2Ftasks%2F1de593d5-b698-4be3-b4e2-ce5ed3031e65",
    );
  });

  it("sends a logged-in visitor straight to the task from /login?next=", () => {
    authState.user = { id: "user-a" };
    renderAt("/login?next=%2Ftasks%2Fabc");
    expect(screen.getByTestId("here").textContent).toBe("/tasks/abc");
  });

  it("ignores an external next target", () => {
    authState.user = { id: "user-a" };
    renderAt("/login?next=https%3A%2F%2Fevil.example");
    expect(screen.getByTestId("here").textContent).toBe("/dashboard");
  });
});
