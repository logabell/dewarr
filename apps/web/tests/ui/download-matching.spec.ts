import { expect, test } from "../fixtures";

for (const scenario of [
  "identified",
  "untagged",
  "ambiguous",
  "retry",
  "automatic",
  "conflict",
  "unknown-conflict",
  "stale-review",
  "unavailable-review",
] as const) {
  test(`download review ${scenario} keeps the decision visible and reuses the saved request`, async ({
    page,
  }, testInfo) => {
    let imported = false;
    let automaticRetries = 0;
    let plans = 0;
    let editions = 0;
    let matchRequests = 0;
    let reviewAvailable = false;
    const submissions: { key: string | undefined; body: unknown }[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const title =
      "The Anxious Generation: How the Great Rewiring of Childhood is Causing an Epidemic of Mental Illness";
    const files =
      scenario === "untagged"
        ? ["01.mp3", "02.mp3"]
        : ["The Anxious Generation.epub"];
    const medium = scenario === "untagged" ? "audio" : "ebook";
    const group = {
      key: "group",
      medium,
      title: "The Anxious Generation",
      authors: ["Jonathan Haidt"],
      narrators: [],
      files: files.map((path) => ({ path, role: "media" })),
    };
    const candidate = {
      work_id: "book",
      version_id: "version",
      identifier_match: true,
      reasons: [
        "Embedded edition identifier matches",
        "Embedded authors agree",
      ],
      conflicts: ["Embedded title is missing or differs"],
      title,
      authors: group.authors,
    };
    const destination = {
      id: "destination",
      medium,
      enabled: true,
      publication_available: true,
      backend_path: "/media/books",
      revision: "route-revision",
      mode: "hardlink",
    };
    const plan = {
      id: "plan",
      inspection_id: "download",
      revision: "plan-revision",
      document: {
        profile: { layout: "conventional" },
        version_revisions: { version: "version-revision" },
        destinations: { [medium]: "destination" },
        plan: {
          items: [
            {
              group_id: "group",
              work_id: "book",
              title,
              medium,
              state: "ready",
              folder: "Jonathan Haidt/The Anxious Generation",
              files: [],
            },
          ],
        },
      },
    };
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let data: unknown = { items: [], total: 0 };
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
      else if (path === "/api/organization/inspections/download")
        data = {
          id: "download",
          state: "ready",
          relative_path: "The Anxious Generation",
          plan_id: imported ? "plan" : null,
          snapshot: {
            revision: "inspection-revision",
            files: files.map((path) => ({ path, medium, state: "inspected" })),
          },
          download: {
            attempt_id: "attempt",
            work_id: "book",
            title,
            authors: group.authors,
            medium,
            destination: "/media/books",
            mode: "hardlink",
            state: imported ? "complete" : "held",
            can_retry: scenario === "automatic",
            destination_id: "destination",
            file_conflicts:
              scenario === "unknown-conflict"
                ? ["The files name a different book"]
                : [],
            message:
              scenario === "untagged"
                ? "Audio files need a complete numbered sequence or matching track totals"
                : "Embedded title is missing or differs",
          },
        };
      else if (path === "/api/organization/inspections/download/retry") {
        automaticRetries++;
        imported = true;
        data = {
          id: "download",
          state: "ready",
          plan_id: "plan",
          download: {
            work_id: "book",
            title,
            authors: group.authors,
            medium,
            state: "complete",
          },
        };
      } else if (path.endsWith("/grouping"))
        data = {
          revision: "grouping-revision",
          content: { groups: [group], excluded: [] },
        };
      else if (path.endsWith("/matches")) {
        matchRequests++;
        expect(
          new URL(route.request().url()).searchParams.get("grouping_revision"),
        ).toBe("grouping-revision");
        if (scenario === "stale-review" && matchRequests === 1)
          return route.fulfill({
            status: 409,
            json: { detail: "File groups changed; reload before matching" },
          });
        if (scenario === "unavailable-review" && !reviewAvailable)
          return route.fulfill({
            status: 503,
            json: { detail: "Review temporarily unavailable" },
          });
        data = {
          items: [
            {
              group_key: "group",
              status: "review",
              revision: "match-revision",
              evidence: {
                issues: [],
                titles: scenario === "conflict" ? ["A Different Book"] : [],
                authors: [],
              },
              candidates:
                scenario === "untagged" || scenario === "unknown-conflict"
                  ? []
                  : scenario === "ambiguous"
                    ? [candidate, { ...candidate, version_id: "other-version" }]
                    : [candidate],
            },
          ],
          total: 1,
        };
      } else if (path === "/api/organization/settings")
        data = { revision: "settings-revision" };
      else if (path === "/api/organization/destinations") data = [destination];
      else if (path === "/api/organization/inspections/download/editions") {
        editions++;
        expect(route.request().postDataJSON().work_id).toBe("book");
        data = { version_id: "version" };
      } else if (path === "/api/organization/inspections/download/plans") {
        plans++;
        const body = route.request().postDataJSON();
        expect(body.selections).toEqual([
          {
            group_key: "group",
            work_id: "book",
            version_id: "version",
            full_content: true,
            contents_confirmed: false,
          },
        ]);
        expect(body.destinations).toEqual({ [medium]: "destination" });
        data = plan;
      } else if (path === "/api/organization/plans/plan") data = plan;
      else if (path === "/api/organization/plans/plan/imports") {
        if (route.request().method() === "POST") {
          submissions.push({
            key: route.request().headers()["idempotency-key"],
            body: route.request().postDataJSON(),
          });
          if (scenario === "retry" && submissions.length === 1)
            return route.fulfill({
              status: 503,
              json: { detail: "Library temporarily unavailable" },
            });
          imported = true;
        }
        data = [
          {
            id: "run",
            plan_id: "plan",
            entries: [
              {
                id: "entry",
                group_id: "group",
                state: "confirmed",
                message: "Available in your library",
                can_retry: false,
                can_cancel: false,
              },
            ],
          },
        ];
      } else if (
        path === "/api/organization/download-roots" ||
        path === "/api/organization/inspections"
      )
        data = [];
      await route.fulfill({ json: data });
    });
    await page.goto("/organization/inspections?inspection=download");
    const review = page.getByRole("region", {
      name: "Download next step",
    });
    await expect(review).toBeVisible();
    if (scenario === "unavailable-review") {
      await expect(
        review.getByText("Review temporarily unavailable"),
      ).toBeVisible();
      reviewAvailable = true;
      await review.getByRole("button", { name: "Refresh review" }).click();
    }
    await expect(page.getByLabel("Find catalog book")).not.toBeVisible();
    await expect(
      page.getByText("Inspection details", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("Files and naming", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("Change book or file selection", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("inspection-revision", { exact: false }),
    ).toHaveCount(0);
    if (
      scenario === "ambiguous" ||
      scenario === "conflict" ||
      scenario === "unknown-conflict"
    ) {
      await expect(
        review.getByRole("link", { name: "Find another download" }),
      ).toHaveAttribute("href", "/books/book?tab=sources");
      await expect(
        review.getByRole("link", { name: "Review book metadata" }),
      ).toHaveAttribute("href", "/books/book?tab=manage");
      await expect(
        review.getByRole("button", { name: "Add to library" }),
      ).toHaveCount(0);
      if (scenario === "conflict")
        await expect(review.getByText("A Different Book")).toBeVisible();
      expect(plans).toBe(0);
      expect(errors).toEqual([]);
      return;
    }
    if (scenario === "automatic") {
      await expect(review.getByRole("status")).toHaveText(
        "Embedded title is missing or differs",
      );
      await review
        .getByRole("button", { name: "Continue automatically" })
        .click();
      await expect(page.locator(".download-import-state")).toHaveText(
        "In library",
      );
      expect(automaticRetries).toBe(1);
      expect(editions).toBe(0);
      expect(plans).toBe(0);
      expect(errors).toEqual([]);
      return;
    }
    await expect(
      review.getByRole("button", { name: "Add to library" }),
    ).toBeDisabled();
    if (scenario === "stale-review" || scenario === "unavailable-review")
      expect(matchRequests).toBe(2);
    await page.screenshot({
      path: testInfo.outputPath("download-review-desktop.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("download-review-mobile.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "View files", exact: true }).click();
    await expect(
      page
        .getByRole("dialog", { name: "Downloaded files" })
        .getByText(files[0], { exact: true }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("button", { name: "View files", exact: true }),
    ).toBeFocused();
    await review.getByLabel("These files contain the complete book").check();
    await review.getByRole("button", { name: "Add to library" }).click();
    if (scenario === "retry") {
      await expect(
        page.getByText("Library temporarily unavailable"),
      ).toBeVisible();
      await review
        .getByRole("button", { name: "Retry import", exact: true })
        .click();
      expect(submissions[1]).toEqual(submissions[0]);
    }
    await expect(page.locator(".download-import-state")).toHaveText(
      "In library",
    );
    expect(plans).toBe(1);
    expect(editions).toBe(scenario === "untagged" ? 1 : 0);
    expect(errors).toEqual([]);
  });
}
