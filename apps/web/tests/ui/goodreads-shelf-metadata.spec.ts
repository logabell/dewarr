import { expect, test } from "../fixtures";

for (const scenario of ["connected", "disabled", "open pending"] as const) {
  const connected = scenario !== "disabled";
  const openPending = scenario === "open pending";
  test(`Goodreads shelf resolves covers with Hardcover ${scenario}`, async ({
    page,
  }) => {
    const workId = "00000000-0000-0000-0000-000000000042";
    const listId = "00000000-0000-0000-0000-000000000007";
    const cover = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="navy"/></svg>')}`;
    const work = {
      id: workId,
      title: "Atmosphere",
      authors: ["Taylor Jenkins Reid"],
      provisional: true,
      cover_url: null,
      description: null,
      availability: { owned: false, ebook: false, audio: false },
    };
    const matchedBook = {
      provider: "hardcover",
      external_id: "42",
      title: "Atmosphere: A Love Story",
      authors: work.authors,
      cover_url: cover,
      description: "Verified Hardcover description",
      series: [],
      editions: [],
      subjects: [],
    };
    let detailLookups = 0;
    let lookups = 0;
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let data: unknown = { items: [], total: 0 };
      if (path === "/api/auth/me")
        data = {
          user: {
            id: "reader",
            role: "viewer",
            username: "reader",
            display_name: "Reader",
          },
          csrf_token: "test",
        };
      else if (path === "/api/following") data = [];
      else if (path === `/api/catalog/works/${workId}`) data = work;
      else if (path === `/api/metadata/works/${workId}`)
        data = {
          sources: [],
          fields: {},
          versions: [],
          versions_total: 0,
          cover_choices: [],
        };
      else if (path === `/api/metadata/works/${workId}/reader-match`) {
        detailLookups++;
        data = { status: "matched", book: matchedBook };
      } else if (path.endsWith("/reader-details"))
        data = { external_id: "42", authors: [], reviews: [] };
      else if (path === "/api/setup/onboarding") data = { status: "completed" };
      else if (path === "/api/metadata/account") data = { enabled: connected };
      else if (path === "/api/discovery/layout")
        data = { order: [], hidden: [] };
      else if (path === "/api/discovery/collections")
        data = { items: [], total: 0 };
      else if (path === "/api/lists/page")
        data = {
          items: [
            {
              id: listId,
              name: "Want to read",
              provider: "goodreads",
              enabled: true,
              books: [work],
            },
          ],
          total: 1,
        };
      else if (path === `/api/lists/${listId}/subscription`)
        data = {
          provider: "goodreads",
          enabled: true,
          state: "idle",
          generation: 1,
        };
      else if (path === `/api/lists/${listId}`)
        data = {
          id: listId,
          name: "Want to read",
          editable: false,
          count: 1,
          matched: 1,
          offset: 0,
          limit: 50,
          items: [work],
          content_revision: "a".repeat(64),
        };
      else if (path === "/api/metadata/reader-matches") {
        lookups++;
        expect(route.request().postDataJSON().work_ids).toEqual([workId]);
        data =
          openPending || lookups === 1
            ? { results: { [workId]: { status: "pending" } } }
            : {
                results: {
                  [workId]: {
                    status: "matched",
                    book: matchedBook,
                  },
                },
              };
      }
      if (
        new URL(route.request().url()).pathname.includes(
          "/acquisition/preferences/",
        )
      )
        data = { effective: { desired_media: "both" } };
      await route.fulfill({ json: data });
    });
    await page.goto("/discover?view=yours");
    const shelf = page.getByRole("region", {
      name: "Want to read followed list",
    });
    if (openPending) {
      await expect.poll(() => lookups).toBeGreaterThan(0);
      await shelf
        .getByRole("link", { name: "View Atmosphere", exact: true })
        .click();
      await expect(page).toHaveURL(new RegExp(`/books/${workId}`));
      await expect(
        page.getByRole("img", { name: "Cover of Atmosphere" }),
      ).toHaveAttribute("src", cover);
      expect(detailLookups).toBe(1);
      expect(errors).toEqual([]);
      return;
    }
    if (connected) {
      await expect(
        shelf.getByRole("img", { name: "Cover of Atmosphere: A Love Story" }),
      ).toHaveAttribute("src", cover);
      await expect(
        shelf.getByRole("heading", { name: "Atmosphere: A Love Story" }),
      ).toBeVisible();
      expect(lookups).toBe(2);
    } else {
      await expect(
        shelf.getByRole("heading", { name: "Atmosphere", exact: true }),
      ).toBeVisible();
      expect(lookups).toBe(0);
    }
    await shelf.getByRole("link", { name: "View all", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`view=yours&list=${listId}`));
    if (connected) {
      await expect(
        page.getByRole("img", { name: "Cover of Atmosphere: A Love Story" }),
      ).toBeVisible();
      expect(lookups).toBe(2);
    }
    expect(errors).toEqual([]);
  });
}
