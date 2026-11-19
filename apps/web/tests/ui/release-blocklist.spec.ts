import { expect, test } from "../fixtures";

const work = {
  id: "work-1",
  title: "The Midnight Train",
  authors: ["Matt Haig"],
  versions: [],
  availability: { owned: false, ebook: false, audio: false, stale: false },
};
const profile = { id: null, generation: 0, preferences: {} };

function release(id: string) {
  return {
    id: `result-${id}`,
    release: {
      source: "mam",
      source_id: id,
      title: `The Midnight Train ${id}`,
      medium: "audio",
      protocol: "torrent",
      authors: ["Matt Haig"],
      narrators: [],
      seeders: 12,
      size_bytes: 492070294,
      format: "m4b",
      details: {},
    },
    assessment: {
      identity: "corroborated",
      blocked: [],
      review: [],
      explanation: [],
      formats: ["m4b"],
      source_origin: "mam",
    },
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    current_connection: true,
    possible_collection: false,
    query_keys: [],
    download: null,
  };
}

async function open(
  page: import("@playwright/test").Page,
  role: "admin" | "member",
  block: Record<string, unknown> | null,
  calls: string[],
  responses: Record<string, unknown> = {},
) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = {};
    let status = 200;
    if (path === "/api/auth/me")
      data = {
        user: { id: "u", role, username: "u", display_name: "U" },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/catalog/works/work-1") data = work;
    else if (path === "/api/acquisition/profiles") data = [profile];
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: {} };
    else if (
      path.endsWith("/source-searches/latest") ||
      path.endsWith("/source-searches")
    )
      data = {
        id: "search-1",
        work_id: work.id,
        request_id: null,
        query: "The Midnight Train",
        medium: "audio",
        offset: 0,
        status: "completed",
        stale_identity: false,
        profile,
        sources: [],
        items: [
          { ...release("1"), block },
          { ...release("2"), block: null },
        ],
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      };
    else if (path.includes("/api/acquisition/recovery/blocklist/")) {
      calls.push(`${request.method()} ${path}`);
      status = request.method() === "DELETE" ? 204 : 200;
      data = request.method() === "DELETE" ? undefined : responses.resume;
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
    await route.fulfill(status === 204 ? { status } : { status, json: data });
  });
  await page.goto("/books/work-1?tab=sources");
  return errors;
}

const block = (canResume: boolean) => ({
  id: "block-1",
  reason: "Completed files differ from the inspected torrent",
  automatic: true,
  blocked_at: "2026-11-18T17:00:00Z",
  can_resume: canResume,
});

test("a blocked release says why, cannot be downloaded, and can be unblocked", async ({
  page,
}) => {
  const calls: string[] = [];
  const errors = await open(page, "admin", block(false), calls);
  const row = page.getByRole("row", { name: "The Midnight Train 1" });
  await expect(row).toContainText("Blocked: Completed files differ");
  await expect(row).toContainText("found automatically");
  await expect(
    row.getByRole("button", { name: "Download The Midnight Train 1" }),
  ).toBeDisabled();
  // The other release in the list is unaffected.
  const other = page.getByRole("row", { name: "The Midnight Train 2" });
  await expect(other).not.toContainText("Blocked");
  await expect(
    other.getByRole("button", { name: "Download The Midnight Train 2" }),
  ).toBeEnabled();
  // Nothing to resume: only the plain action is offered.
  await expect(
    row.getByRole("button", { name: "Unblock and resume" }),
  ).toHaveCount(0);
  await row.getByRole("button", { name: "Unblock", exact: true }).click();
  await expect
    .poll(() => calls)
    .toEqual(["DELETE /api/acquisition/recovery/blocklist/block-1"]);
  expect(errors).toEqual([]);
});

test("an interrupted transfer offers Unblock and resume and reports the outcome", async ({
  page,
}) => {
  const calls: string[] = [];
  const errors = await open(page, "admin", block(true), calls, {
    resume: {
      block_id: "block-1",
      unblocked: true,
      resumed: 1,
      attempts: [
        { attempt_id: "attempt-1", resumed: true, message: "Resumed" },
      ],
    },
  });
  const row = page.getByRole("row", { name: "The Midnight Train 1" });
  await row.getByRole("button", { name: "Unblock and resume" }).click();
  await expect(row).toContainText("no new torrent was added");
  expect(calls).toEqual([
    "POST /api/acquisition/recovery/blocklist/block-1/resume",
  ]);
  expect(errors).toEqual([]);
});

test("a refused resume still unblocks and explains why", async ({ page }) => {
  const calls: string[] = [];
  await open(page, "admin", block(true), calls, {
    resume: {
      block_id: "block-1",
      unblocked: true,
      resumed: 0,
      attempts: [
        {
          attempt_id: "attempt-1",
          resumed: false,
          message:
            "A replacement search or download already followed this failure",
        },
      ],
    },
  });
  const row = page.getByRole("row", { name: "The Midnight Train 1" });
  await row.getByRole("button", { name: "Unblock and resume" }).click();
  await expect(row).toContainText(
    "Unblocked, but the existing download was not resumed",
  );
  await expect(row).toContainText("already followed this failure");
  await expect(row).not.toContainText("Resumed. Dewarr");
});

test("members see why a release is blocked but cannot unblock it", async ({
  page,
}) => {
  const calls: string[] = [];
  await open(page, "member", block(true), calls);
  const row = page.getByRole("row", { name: "The Midnight Train 1" });
  await expect(row).toContainText("Blocked: Completed files differ");
  await expect(row.getByRole("button", { name: /Unblock/ })).toHaveCount(0);
  expect(calls).toEqual([]);
});
