import { expect, test } from "../fixtures";

test("the Discover calendar has one main heading", async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data: unknown = [];
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "member",
          display_name: "Reader",
          permissions: [],
          access_label: "Member",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/releases/calendar")
      data = {
        month: url.searchParams.get("month"),
        genres: [],
        choices: ["Fantasy"],
        items: [],
        undated: [],
        page: 1,
        has_more: false,
        status: "ready",
        stale: false,
      };
    return route.fulfill({ json: data });
  });
  await page.goto("/discover?view=calendar");
  await expect(
    page.getByRole("heading", { level: 2, name: "Calendar" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Discover");
});
