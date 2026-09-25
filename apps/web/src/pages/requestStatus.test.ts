import assert from "node:assert/strict";
import { test } from "node:test";
import { statusLabel } from "./requestStatus.ts";

const active = {
  approval_status: "approved",
  reasons: [{ active: true }],
};

test("a finished transfer still being imported is Importing", () => {
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      attempt_state: "complete",
      next_action: "downloads",
    }),
    "Importing",
  );
});

test("an active transfer stays Downloading", () => {
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      attempt_state: "downloading",
      next_action: "downloads",
    }),
    "Downloading",
  );
});

test("a committed selection with no transfer yet is Downloading", () => {
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      next_action: "downloads",
    }),
    "Downloading",
  );
});

test("a cancelled transfer is no longer Downloading", () => {
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      attempt_state: "cancelled",
      next_action: "downloads",
    }),
    "Wanted",
  );
});

test("a finished download waiting on inventory is Check inventory", () => {
  assert.equal(
    statusLabel(active, {
      state: "awaiting-inventory",
      attempt_state: "complete",
      next_action: "downloads",
      message: "Refresh library inventory before acquiring another copy",
    }),
    "Check inventory",
  );
});

test("a completed transfer already in the library is In library", () => {
  assert.equal(
    statusLabel(active, {
      state: "satisfied",
      attempt_state: "complete",
      next_action: "book",
    }),
    "In library",
  );
});

test("manual release failures and preparation remain visible in Requests", () => {
  assert.equal(
    statusLabel(active, { state: "wanted", selection_status: "held" }),
    "Download not started",
  );
  assert.equal(
    statusLabel(active, { state: "wanted", selection_status: "failed" }),
    "Download not started",
  );
  assert.equal(
    statusLabel(active, { state: "wanted", selection_status: "running" }),
    "Preparing download",
  );
  assert.equal(
    statusLabel(active, { state: "satisfied", selection_status: "held" }),
    "In library",
  );
});

test("a held import needs review and a satisfied request wins over stale review", () => {
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      attempt_state: "complete",
      import_state: "held",
    }),
    "Needs review",
  );
  assert.equal(
    statusLabel(active, {
      state: "wanted",
      attempt_state: "complete",
      needs_review: true,
    }),
    "Needs review",
  );
  assert.equal(
    statusLabel(active, {
      state: "satisfied",
      attempt_state: "complete",
      needs_review: true,
    }),
    "In library",
  );
});
