import { test as base } from "@playwright/test";
export { expect, type Page } from "@playwright/test";

// Cover appearance tests exercise layout and URL selection, not remote image CDNs.
// Individual tests can override this route to test failures or specific artwork.
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (
        route.request().resourceType() === "image" &&
        url.protocol.startsWith("http") &&
        (!["127.0.0.1", "localhost"].includes(url.hostname) ||
          url.pathname === "/api/catalog/cover-image")
      ) {
        await route.fulfill({
          contentType: "image/svg+xml",
          body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="#765"/></svg>',
        });
      } else await route.fallback();
    });
    await use(page);
  },
});

export function emptyDiscoveryHome() {
  const index = {
    items: [],
    total: 0,
    years: [],
    genres: [],
    categories: [],
    archive_gaps: [],
  };
  return {
    index,
    public_lists: index,
    saved: index,
    layout: { order: [], hidden: [] },
    selected: [],
  };
}
