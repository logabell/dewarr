import { expect, test } from "../fixtures";

test("Discover defers full shelf enumeration until Customize and retains a selected shelf", async ({
  page,
}) => {
  const fullPages: number[] = [];
  const selectedQueries: string[][] = [];
  const collection = (id: string) => ({
    id,
    title: `Collection ${id}`,
    kind: "listopia",
    genres: [],
    covers: [],
    count: 0,
    saved: false,
    pinned: false,
    tracking: false,
    source_url: "https://goodreads.com/list/show/1",
    coverage: "complete",
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        csrf_token: "qa",
        user: {
          id: "reader",
          username: "reader",
          role: "admin",
          display_name: "Reader",
        },
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/discovery/layout")
      data = { order: ["special"], hidden: [] };
    else if (url.pathname === "/api/metadata/account")
      data = { enabled: false };
    else if (url.pathname === "/api/discovery/collections") {
      const ids = url.searchParams.getAll("ids");
      if (ids.length) {
        selectedQueries.push(ids);
        data = { items: [collection("special")], total: 1 };
      } else if (url.searchParams.has("page")) {
        const pageNumber = Number(url.searchParams.get("page"));
        fullPages.push(pageNumber);
        data = {
          items: Array.from(
            { length: pageNumber === 3 ? 1 : 100 },
            (_, index) => collection(String((pageNumber - 1) * 100 + index)),
          ),
          total: 201,
        };
      }
    } else if (url.pathname === "/api/discovery/collections/special")
      data = {
        collection: collection("special"),
        items: [],
        total: 0,
        page: 1,
        has_more: false,
      };
    await route.fulfill({ json: data });
  });
  await page.goto("/discover");
  await expect(
    page.getByRole("heading", { name: "Collection special", exact: true }),
  ).toBeVisible();
  expect(selectedQueries).toEqual([["special"]]);
  expect(fullPages).toEqual([]);
  await page.getByRole("button", { name: "Customize", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Customize Discover" });
  await expect(dialog).toBeVisible();
  expect(fullPages).toEqual([1, 2, 3]);
  await dialog
    .getByRole("button", { name: "Add shelves", exact: true })
    .click();
  await dialog
    .getByRole("textbox", { name: "Search available shelves" })
    .fill("Collection 200");
  await expect(
    dialog.getByRole("button", { name: "Add Collection 200", exact: true }),
  ).toBeVisible();
});
