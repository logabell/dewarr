import { expect, test, emptyDiscoveryHome } from "../fixtures";

test("Discover starts six shelves independently and keeps pending requests when lists arrive", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1100, height: 500 });
  const started: string[] = [];
  const aborted: string[] = [];
  let finishShelves!: () => void;
  let finishLists!: () => void;
  const pendingShelves = new Promise<void>((resolve) => {
    finishShelves = resolve;
  });
  const pendingLists = new Promise<void>((resolve) => {
    finishLists = resolve;
  });
  const collections = Array.from({ length: 20 }, (_, i) => ({
    id: `shelf-${i}`,
    title: `Shelf ${i}`,
    kind: "listopia",
    provider: "goodreads",
    genres: [],
    covers: [],
    count: 1,
    saved: true,
    pinned: true,
    tracking: false,
    source_url: `https://goodreads.com/list/show/${i + 1}`,
  }));
  const lateList = {
    id: "late",
    name: "Late personal list",
    editable: false,
    count: 0,
  };
  const layout = {
    order: ["personal:late", ...collections.map((c) => c.id)],
    hidden: ["shelf-6", "library"],
  };
  page.on("requestfailed", (request) => {
    if (request.url().includes("/api/discovery/collections/shelf-"))
      aborted.push(new URL(request.url()).pathname.split("/").at(-1)!);
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/metadata/account")
      data = { enabled: false };
    else if (url.pathname === "/api/discovery/home")
      data = {
        ...emptyDiscoveryHome(),
        layout,
        selected: collections,
        saved: { items: collections, total: collections.length },
      };
    else if (url.pathname === "/api/discovery/layout") data = layout;
    else if (url.pathname === "/api/lists/page") {
      await pendingLists;
      data = { items: [lateList], total: 1 };
    } else if (url.pathname === "/api/lists/late")
      data = { ...lateList, items: [] };
    else if (url.pathname.startsWith("/api/discovery/collections/shelf-")) {
      const id = url.pathname.split("/").at(-1)!;
      started.push(id);
      // The sixth shelf can paint even while all earlier shelves are blocked.
      if (id !== "shelf-5") await pendingShelves;
      data = {
        collection: collections.find((c) => c.id === id),
        items: [
          {
            provider: "goodreads",
            external_id: id,
            title: `Book ${id}`,
            authors: ["Writer"],
            cover_url: null,
          },
        ],
        total: 1,
        page: 1,
        has_more: false,
      };
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/discover");
  try {
    await expect
      .poll(() => started)
      .toEqual(
        expect.arrayContaining(collections.slice(0, 6).map((c) => c.id)),
      );
    await expect(
      page.getByRole("heading", { name: "Book shelf-5", exact: true }),
    ).toBeAttached();
    await expect(
      page.getByRole("heading", { name: "Book shelf-0", exact: true }),
    ).toHaveCount(0);
    finishLists();
    await expect(
      page.getByRole("heading", { name: "Late personal list", exact: true }),
    ).toBeVisible();
  } finally {
    finishLists();
    finishShelves();
  }
  await expect(
    page.getByRole("heading", { name: "Book shelf-0", exact: true }),
  ).toBeVisible();
  expect(aborted).toEqual([]);
  expect(started.filter((id) => id === "shelf-1")).toHaveLength(1);
  expect(started).not.toContain("shelf-6");
  // Far-off shelves remain deferred instead of flooding the connection.
  expect(started).not.toContain("shelf-19");
  await page
    .getByRole("heading", { name: "Shelf 19", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    page.getByRole("heading", { name: "Book shelf-19", exact: true }),
  ).toBeVisible();
});

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
    else if (url.pathname === "/api/discovery/home")
      data = {
        ...emptyDiscoveryHome(),
        layout: { order: ["special"], hidden: [] },
        selected: [collection("special")],
      };
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
  expect(selectedQueries).toEqual([]);
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

test("a saved collection paints before its full source page finishes", async ({
  page,
}) => {
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => {
    finish = resolve;
  });
  let fullStarted = false;
  const collection = {
    id: "gr-list-50",
    title: "Epic fantasy",
    kind: "listopia",
    provider: "goodreads",
    genres: [],
    covers: [],
    count: 500,
    pinned: false,
    tracking: false,
    source_url: "https://goodreads.com/list/show/50",
  };
  const entry = (title: string) => ({
    provider: "goodreads",
    external_id: "1",
    title,
    authors: ["Writer"],
    cover_url: null,
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/discovery/collections/gr-list-50") {
      const full = url.searchParams.get("full") === "true";
      if (full) {
        fullStarted = true;
        await pending;
      }
      data = {
        collection,
        items: [entry(full ? "Updated book" : "Saved book")],
        total: full ? 500 : 1,
        page: 1,
        has_more: false,
      };
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/discover/collections/gr-list-50");
  try {
    await expect(
      page.getByRole("heading", { name: "Saved book", exact: true }),
    ).toBeVisible();
    expect(fullStarted).toBe(true);
  } finally {
    finish();
  }
  await expect(
    page.getByRole("heading", { name: "Updated book", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Saved book", exact: true }),
  ).toHaveCount(0);
});
