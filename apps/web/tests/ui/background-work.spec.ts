import { expect, test } from "../fixtures";

test("admins choose how often background jobs run", async ({ page }) => {
  const saved: unknown[] = [];
  let pace = "normal";
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = { items: [], total: 0, offset: 0, limit: 40 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role: "admin",
          username: "admin",
          display_name: "Admin",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/library/libraries") data = [];
    else if (path === "/api/background-work") {
      if (request.method() === "PUT") {
        saved.push(request.postDataJSON());
        pace = (request.postDataJSON() as { pace: string }).pace;
      }
      data = { pace };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#background-work");
  const section = page.getByRole("region", { name: "Background work" });
  await expect(section.getByText("New in this version.")).toBeVisible();
  await expect(section.getByRole("radio", { name: /^Normal/ })).toBeChecked();
  await section.getByRole("radio", { name: /^Light/ }).check();
  await section.getByRole("button", { name: "Save background work" }).click();
  await expect(section.getByRole("status")).toHaveText(
    "Background work saved. It applies within a minute.",
  );
  expect(saved).toEqual([{ pace: "light" }]);
  await page.reload();
  await expect(
    page
      .getByRole("region", { name: "Background work" })
      .getByRole("radio", { name: /^Light/ }),
  ).toBeChecked();
});
