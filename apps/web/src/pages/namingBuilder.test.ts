import assert from "node:assert/strict";
import { test } from "node:test";
import {
  filenameStyles,
  illustrate,
  parseSegment,
  readChoices,
  seriesIndexFilename,
  seriesIndexFolder,
  templateSegments,
  toggleSegment,
  withJoin,
} from "./namingBuilder.ts";

test("sequence title layout keeps sequence and title in one folder", () => {
  assert.deepEqual(templateSegments(seriesIndexFolder), [
    "{author}/",
    "[{series}/]",
    "[{sequence} ]",
    "{title}",
  ]);
  assert.deepEqual(templateSegments(seriesIndexFilename), [
    "[{sequence} - ]",
    "[{series} - ]",
    "{title}",
    "[ ({year})]",
  ]);
  const choices = readChoices(seriesIndexFolder, "audio");
  assert.equal(choices?.author, true);
  assert.equal(choices?.series, true);
  assert.equal(choices?.sequence, true);
  assert.equal(choices?.year, false);
});

test("join controls build a space-separated sequence folder and a wrapped year", () => {
  assert.equal(withJoin("[{sequence} - ]", "space"), "[{sequence} ]");
  assert.equal(withJoin("[{sequence} - ]", "folder"), "[{sequence}/]");
  assert.equal(withJoin("[ ({year})]", "dash"), "[ - {year}]");
  assert.equal(withJoin("[ - {narrator}]", "parentheses"), "[ ({narrator})]");
  assert.equal(withJoin("{author}/", "dash"), "{author} - ");
  assert.equal(withJoin("[{disc}-]", "space"), "[{disc}-]");
  assert.equal(parseSegment("{title}")?.join, null);
  assert.equal(withJoin("[ - {narrator}]", "braces"), "[ {{{narrator}}}]");
  assert.equal(withJoin("[ {{{narrator}}}]", "dash"), "[ - {narrator}]");
  assert.equal(
    illustrate("{title}[ {{{narrator}}}]", "audio"),
    "Philosopher’s Stone {Stephen Fry}",
  );
});

test("style examples show the punctuation each choice changes", () => {
  assert.equal(illustrate("{title}", "ebook"), "Philosopher’s Stone");
  assert.equal(
    illustrate("[{sequence} - ]{title}", "ebook"),
    "01 - Philosopher’s Stone",
  );
  assert.equal(
    illustrate(seriesIndexFolder, "ebook"),
    "J. K. Rowling/Harry Potter/01 Philosopher’s Stone",
  );
  assert.equal(
    illustrate(seriesIndexFilename, "audio"),
    "01 - Harry Potter - Philosopher’s Stone (1999)",
  );
  assert.equal(
    illustrate("[{disc}-][{track} - ]{title}", "audio"),
    "01-001 - Philosopher’s Stone",
  );
  assert.deepEqual(
    filenameStyles("ebook").map((style) => style.name),
    [
      "Title only",
      "Author and title",
      "Number and title",
      "Number, series, and year",
    ],
  );
  assert.equal(filenameStyles("audio")[0].name, "Disc and track");
});

test("the year checkbox treats the shared year token as the medium year", () => {
  assert.equal(
    toggleSegment("{author}/[ ({year})]{title}", "ebook", "year"),
    "{author}/{title}",
  );
  assert.equal(readChoices("{author}/[ ({year})]{title}", "audio")?.year, true);
  assert.equal(
    toggleSegment("{author}/{title}", "ebook", "year"),
    "{author}/[{edition_year} - ]{title}",
  );
});
