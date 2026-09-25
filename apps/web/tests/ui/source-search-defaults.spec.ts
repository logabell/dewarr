import { expect, test } from "../fixtures";

test("manual search shares defaults and confirmed Quick Add feedback retires", async ({
  page,
}) => {
  const work = {
    id: "work-1",
    title:
      "Enshittification: Why Everything Suddenly Got Worse and What to Do About It",
    authors: ["Cory Doctorow"],
    versions: [],
    availability: { owned: false, ebook: false, audio: false, stale: false },
  };
  const profile = { id: null, generation: 0, preferences: {} };
  const searches: Record<string, unknown>[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let saved: Record<string, unknown> | null = null;
  let receipt: Record<string, unknown> | null = {
    id: "quick-1",
    request_id: "request-1",
    status: "completed",
    message: "Audiobook: automatic download queued",
    source_checks: [],
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = {};
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/catalog/works/work-1") data = work;
    else if (path === "/api/acquisition/profiles") data = [profile];
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: {} };
    else if (path.includes("/quick-add/latest/")) data = receipt;
    else if (path.endsWith("/source-searches/latest")) data = saved;
    else if (path.endsWith("/source-searches")) {
      const body = route.request().postDataJSON();
      searches.push(body);
      saved = {
        id: `search-${searches.length}`,
        work_id: work.id,
        request_id: null,
        query: body.q ?? "Enshittification",
        medium: "all",
        offset: 0,
        status: "completed",
        stale_identity: false,
        profile,
        sources: [],
        items: [],
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      };
      data = saved;
    } else if (path === "/api/metadata/works/work-1")
      data = {
        sources: [],
        conflicts: [],
        fields: {},
        versions: [],
        versions_total: 0,
      };
    else if (path.endsWith("/reader-match")) data = { book: null };
    else if (path.endsWith("/reader-details"))
      data = { authors: [], reviews: [] };
    else if (path.endsWith("/versions")) data = [];
    else if (path.includes("/group"))
      data = { items: [], groups: [], members: [] };
    else if (path === "/api/requests" || path === "/api/catalog/works")
      data = { items: [], total: 0 };
    await route.fulfill({ json: data });
  });
  await page.clock.install();
  await page.goto("/books/work-1?tab=sources");
  const query = page.getByRole("textbox", { name: "Release search query" });
  await expect(query).toHaveValue("Enshittification");
  const feedback = page.getByRole("region", { name: "Quick add progress" });
  await expect(feedback).toContainText("automatic download queued");
  expect(searches).toHaveLength(1);
  expect(searches[0]).not.toHaveProperty("q");
  await query.fill("Enshittification Cory Doctorow epub");
  await page.getByRole("button", { name: "Refresh source results" }).click();
  await expect.poll(() => searches.length).toBe(2);
  expect(searches[1].q).toBe("Enshittification Cory Doctorow epub");
  // The API returns no current receipt after inventory confirms every format.
  // The banner must disappear on the open page, without a manual reload.
  receipt = null;
  await page.clock.fastForward(5000);
  await expect(feedback).toHaveCount(0);
  await page.reload();
  await expect(query).toHaveValue("Enshittification Cory Doctorow epub");
  expect(searches).toHaveLength(2);
  await expect(feedback).toHaveCount(0);
  expect(errors).toEqual([]);
});
