import { expect, test } from "../fixtures";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { items: [], total: 0, statuses: [], kinds: [] } }),
  );
  await page.route("**/api/request-quotas/me", (route) =>
    route.fulfill({
      json: { bypass: false, windows: [], pending_remaining: null },
    }),
  );
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: {
        csrf_token: "test-token",
        recovery: false,
        user: {
          id: "reader",
          role: "member",
          display_name: "Reader",
          onboarding_status: "completed",
          permissions: [],
        },
      },
    }),
  );
});

const subscriptions = [
  {
    list_id: "one",
    name: "Want to read",
    subscription: {
      provider: "goodreads",
      enabled: true,
      state: "idle",
      message: "Updated",
    },
  },
  {
    list_id: "two",
    name: "Favorites",
    subscription: {
      provider: "goodreads",
      enabled: true,
      state: "idle",
      message: "Updated",
    },
  },
  {
    list_id: "paused",
    name: "Paused",
    subscription: { provider: "goodreads", enabled: false, state: "paused" },
  },
  {
    list_id: "hardcover",
    name: "Want to read",
    subscription: { provider: "hardcover", enabled: true, state: "idle" },
  },
  {
    list_id: "storygraph",
    name: "To-read",
    subscription: { provider: "storygraph", enabled: true, state: "idle" },
  },
];

for (const scenario of [
  "success",
  "empty",
  "partial failure",
  "worker failure",
]) {
  test(`Goodreads topbar refresh: ${scenario}`, async ({ page }) => {
    const calls: string[] = [];
    let finished = false;
    await page.route("**/api/reading-accounts/subscriptions", (route) =>
      route.fulfill({
        json:
          scenario === "empty"
            ? subscriptions.map((item) => ({
                ...item,
                subscription: { ...item.subscription, enabled: false },
              }))
            : subscriptions.map((item) => ({
                ...item,
                subscription: {
                  ...item.subscription,
                  state: calls.includes(item.list_id)
                    ? !finished
                      ? "running"
                      : scenario === "worker failure" && item.list_id === "two"
                        ? "failed"
                        : "idle"
                    : item.subscription.state,
                  message:
                    scenario === "worker failure"
                      ? "Goodreads unavailable"
                      : "Updated",
                },
              })),
      }),
    );
    await page.route("**/api/lists/*/subscription/sync", (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["idempotency-key"]).toBeTruthy();
      const id = route.request().url().split("/").at(-3)!;
      calls.push(id);
      return scenario === "partial failure" && id === "one"
        ? route.fulfill({ status: 409, json: { detail: "Shelf was paused" } })
        : route.fulfill({ status: 202, json: { id, status: "queued" } });
    });
    await page.goto("/requests");
    const button = page.getByRole("button", {
      name: "Refresh lists",
      exact: true,
    });
    await expect(
      page
        .locator(".topbar-actions")
        .getByRole("button", { name: "Add list", exact: true }),
    ).toBeVisible();
    await button.click();
    if (scenario !== "empty") {
      await expect
        .poll(() => calls)
        .toEqual(["one", "two", "hardcover", "storygraph"]);
      await expect(button).toBeDisabled();
      finished = true;
    }
    const expected =
      scenario === "empty"
        ? "No enabled reading lists."
        : scenario === "partial failure"
          ? "Want to read: Shelf was paused"
          : scenario === "worker failure"
            ? "Favorites: Goodreads unavailable"
            : "Refreshed 4 lists.";
    await expect(
      page.getByRole("status").filter({ hasText: expected }),
    ).toBeVisible();
    await expect(button).toBeEnabled();
    expect(calls).toEqual(
      scenario === "empty" ? [] : ["one", "two", "hardcover", "storygraph"],
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(button).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("topbar refresh can update StoryGraph lists only", async ({ page }) => {
  const calls: string[] = [];
  await page.route("**/api/reading-accounts/subscriptions", (route) =>
    route.fulfill({
      json: subscriptions.map((item) => ({
        ...item,
        subscription: {
          ...item.subscription,
          state: calls.includes(item.list_id)
            ? "idle"
            : item.subscription.state,
          message: "Updated",
        },
      })),
    }),
  );
  await page.route("**/api/lists/*/subscription/sync", (route) => {
    calls.push(route.request().url().split("/").at(-3)!);
    return route.fulfill({ status: 202, json: { status: "queued" } });
  });
  await page.goto("/requests");
  await page
    .getByRole("button", { name: "Choose which lists to refresh" })
    .click();
  await page.getByRole("menuitem", { name: "StoryGraph", exact: true }).click();
  await expect.poll(() => calls).toEqual(["storygraph"]);
  await expect(page.getByText("Refreshed 1 StoryGraph list.")).toBeVisible();
});
