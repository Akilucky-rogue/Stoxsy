import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../lib/auth";
import { setAccessToken } from "../lib/api";
import { Login } from "./Auth";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const me = (over = {}) => ({ id: "1", email: "a@b.co", role: "admin", authenticated: true, must_change_password: false, auth_enforced: true, ...over });

function mount() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<div>HOME</div>} />
          <Route path="/change-password" element={<div>CHANGE</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => { vi.restoreAllMocks(); setAccessToken(null); });

describe("sign-in", () => {
  it("shows the form when sign-in is required, shows the server error, then enters on success", async () => {
    let signedIn = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const u = String(url);
      if (u === "/api/auth/me") return signedIn ? json(200, me()) : json(401, { detail: "not authenticated" });
      if (u === "/api/auth/refresh") return json(401, { detail: "no session" });
      if (u === "/api/auth/login") {
        const b = JSON.parse(String((init as RequestInit).body));
        if (b.password !== "right password 123") return json(401, { detail: "invalid email or password" });
        signedIn = true;
        return json(200, { access_token: "tok", expires_in: 900, must_change_password: false, user: { id: "1", email: "a@b.co", name: "", role: "admin", status: "active", must_change_password: false } });
      }
      return json(404, {});
    });
    mount();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "a@b.co");
    await user.type(screen.getByLabelText("Password"), "wrong password 1");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid email or password");
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "right password 123");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(screen.getByText("HOME")).toBeInTheDocument());
  });

  it("goes straight in when the backend runs in local mode (auth not enforced)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, me({ authenticated: false, auth_enforced: false })));
    mount();
    await waitFor(() => expect(screen.getByText("HOME")).toBeInTheDocument());
  });

  it("forces a password change for a temporary password", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const u = String(url);
      if (u === "/api/auth/me") return json(200, me({ must_change_password: true }));
      return json(404, {});
    });
    mount();
    await waitFor(() => expect(screen.getByText("CHANGE")).toBeInTheDocument());
  });
});
