import assert from "node:assert/strict";
import { test } from "node:test";
import { searchTitle, titlePart } from "./titleLabels.ts";

test("search text drops part numbers and recording labels", () => {
  assert.equal(
    searchTitle("Dark Age (1 of 3) [Dramatized Adaptation]"),
    "Dark Age",
  );
  assert.equal(
    searchTitle("Golden Son (Part 1 of 2) (Dramatized Adaptation)"),
    "Golden Son",
  );
  assert.equal(
    searchTitle("Mistborn 7: The Lost Metal 1 of 2"),
    "Mistborn 7: The Lost Metal",
  );
  assert.equal(
    searchTitle("Storm Front: Dramatized Adaptation"),
    "Storm Front",
  );
  assert.equal(searchTitle("1 of 3"), "1 of 3");
});

test("part numbers must describe a real set", () => {
  assert.deepEqual(
    titlePart("Dark Age (3 of 3) [Dramatized Adaptation]"),
    [3, 3],
  );
  assert.equal(titlePart("Dark Age (4 of 3)"), null);
  assert.equal(titlePart("Dark Age"), null);
});
