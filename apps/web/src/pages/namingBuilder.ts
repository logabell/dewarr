import type { components } from "../api/schema";
export type NamingProfile = components["schemas"]["NamingProfile"];
export type Medium = "audio" | "ebook";
export type NamingChoices = {
  author: boolean;
  series: boolean;
  sequence: boolean;
  year: boolean;
  version: boolean;
  language: boolean;
  publisher: boolean;
};
export const simpleChoices: NamingChoices = {
  author: true,
  series: false,
  sequence: false,
  year: false,
  version: false,
  language: false,
  publisher: false,
};
export const seriesChoices: NamingChoices = {
  ...simpleChoices,
  series: true,
  sequence: true,
};
export const detailedChoices: NamingChoices = {
  ...seriesChoices,
  year: true,
  version: true,
};
export function folderTemplate(medium: Medium, choices: NamingChoices) {
  return `${choices.author ? "{author}/" : ""}${choices.series ? "[{series}/]" : ""}${choices.sequence ? "[{sequence} - ]" : ""}${choices.year ? (medium === "audio" ? "[{recording_year} - ]" : "[{edition_year} - ]") : ""}{title}${choices.version ? (medium === "audio" ? "[ - {narrator}]" : "[ - {edition}]") : ""}${choices.language ? "[ - {language}]" : ""}${choices.publisher ? "[ - {publisher}]" : ""}`;
}
export function readChoices(
  template: string,
  medium: Medium,
): NamingChoices | null {
  const choices = {
    author: template.includes("{author}"),
    series: template.includes("{series}"),
    sequence: template.includes("{sequence}"),
    year:
      template.includes("{year}") ||
      template.includes(
        medium === "audio" ? "{recording_year}" : "{edition_year}",
      ),
    version: template.includes(medium === "audio" ? "{narrator}" : "{edition}"),
    language: template.includes("{language}"),
    publisher: template.includes("{publisher}"),
  };
  const segments = templateSegments(template);
  const tokens = segments.map(
    (segment) => escapedTemplate(segment).match(/\{[a-z_]+\}/g) || [],
  );
  const supported = new Set([
    "{title}",
    "{author}",
    "{series}",
    "{sequence}",
    "{edition_year}",
    "{recording_year}",
    "{year}",
    "{edition}",
    "{narrator}",
    "{language}",
    "{publisher}",
  ]);
  return tokens.every(
    (group) => group.length === 1 && supported.has(group[0]),
  ) && new Set(tokens.flat()).size === tokens.length
    ? choices
    : null;
}
// Sent to the same planner as real imports; these examples never enter the catalog.
export const namingExamples: components["schemas"]["ImportGroup"][] = (
  ["ebook", "audio"] as const
).map((medium, index) => ({
  id: `00000000-0000-0000-0000-00000000000${index + 1}`,
  work_id: "00000000-0000-0000-0000-000000000010",
  version_id: `00000000-0000-0000-0000-00000000002${index}`,
  medium,
  decision: "import" as const,
  full_content: true,
  metadata: {
    title: "Harry Potter and the Philosopher’s Stone",
    authors: ["J. K. Rowling"],
    series: "Harry Potter",
    sequence: "1",
    original_year: 1997,
    edition_year: 1997,
    recording_year: 1999,
    narrators: ["Stephen Fry"],
    edition: "First edition",
    publisher: "Bloomsbury",
    language: "English",
  },
  files: [
    {
      role: "media" as const,
      complete: true,
      path:
        medium === "audio"
          ? "Harry Potter/01 - The Boy Who Lived.mp3"
          : "Harry Potter.epub",
      ...(medium === "audio" ? { track: 1, disc: 1 } : {}),
    },
  ],
}));

// Keep punctuation and optional brackets attached when moving metadata segments.
export function templateSegments(template: string): string[] {
  const parts =
    template.match(/\[[^\]]*\]|[^\[\]{}]*\{[^}]+\}[^\[\]{}]*/g) || [];
  return parts.join("") === template && new Set(parts).size === parts.length
    ? parts
    : [template];
}
const YEAR_TOKENS = ["year", "recording_year", "edition_year"];
export const seriesIndexFolder = "{author}/[{series}/][{sequence} ]{title}";
export const seriesIndexFilename =
  "[{sequence} - ][{series} - ]{title}[ ({year})]";

const SAMPLE: Record<string, string> = {
  author: "J. K. Rowling",
  title: "Philosopher’s Stone",
  series: "Harry Potter",
  sequence: "01",
  edition_year: "1997",
  recording_year: "1999",
  edition: "First edition",
  narrator: "Stephen Fry",
  language: "English",
  publisher: "Bloomsbury",
  disc: "01",
  track: "001",
};

// Short stand-in for the style cards. The live preview still comes from the planner.
function escapedTemplate(template: string): string {
  return template.replace(/\{\{|\}\}|\{[a-z_]+\}/g, (part) =>
    part === "{{" ? "\u0001" : part === "}}" ? "\u0002" : part,
  );
}

export function illustrate(template: string, medium: Medium): string {
  const values: Record<string, string> = {
    ...SAMPLE,
    year: medium === "audio" ? SAMPLE.recording_year : SAMPLE.edition_year,
  };
  const escaped = escapedTemplate(template);
  const filled = escaped.replace(/\[[^\]]*\]/g, (block) => {
    const tokens = [...block.matchAll(/\{([a-z_]+)\}/g)].map(
      (match) => match[1],
    );
    return tokens.length > 0 && tokens.every((token) => values[token])
      ? block.slice(1, -1)
      : "";
  });
  return filled
    .replace(/\{([a-z_]+)\}/g, (_, token: string) => values[token] || "")
    .replaceAll("\u0001", "{")
    .replaceAll("\u0002", "}");
}

export function filenameStyles(medium: Medium) {
  const styles = [
    { name: "Title only", template: "{title}" },
    { name: "Author and title", template: "{author} - {title}" },
    { name: "Number and title", template: "[{sequence} - ]{title}" },
    { name: "Number, series, and year", template: seriesIndexFilename },
  ];
  if (medium === "audio")
    styles.unshift({
      name: "Disc and track",
      template: "[{disc}-][{track} - ]{title}",
    });
  return styles;
}
export type TokenJoin = "folder" | "dash" | "space" | "parentheses" | "braces";
export type ParsedSegment = {
  optional: boolean;
  token: string;
  join: TokenJoin | null;
  leading: boolean;
};
const JOINS: [string, string, TokenJoin, boolean][] = [
  ["", "/", "folder", false],
  ["", " - ", "dash", false],
  ["", " ", "space", false],
  [" - ", "", "dash", true],
  [" ", "", "space", true],
  [" (", ")", "parentheses", true],
  [" {{", "}}", "braces", true],
];
export function parseSegment(segment: string): ParsedSegment | null {
  const optional = segment.startsWith("[") && segment.endsWith("]");
  const body = optional ? segment.slice(1, -1) : segment;
  const match = body.match(/^(.*?)\{([a-z_]+)\}(.*?)$/);
  if (!match) return null;
  const [, prefix, token, suffix] = match;
  const found = JOINS.find(
    ([knownPrefix, knownSuffix]) =>
      prefix === knownPrefix && suffix === knownSuffix,
  );
  if (!found && (prefix !== "" || suffix !== "")) return null;
  return {
    optional,
    token,
    join: found ? found[2] : null,
    leading: found ? found[3] : false,
  };
}
export function formatSegment({
  optional,
  token,
  join,
  leading,
}: ParsedSegment): string {
  let prefix = "";
  let suffix = "";
  if (join === "folder") suffix = "/";
  else if (join === "parentheses") {
    prefix = " (";
    suffix = ")";
  } else if (join === "braces") {
    prefix = " {{";
    suffix = "}}";
  } else if (join === "dash") {
    if (leading) prefix = " - ";
    else suffix = " - ";
  } else if (join === "space") {
    if (leading) prefix = " ";
    else suffix = " ";
  }
  const body = `${prefix}{${token}}${suffix}`;
  return optional ? `[${body}]` : body;
}
export function withJoin(segment: string, join: TokenJoin): string {
  const parsed = parseSegment(segment);
  if (!parsed) return segment;
  const leading =
    join === "folder"
      ? false
      : join === "parentheses" ||
          join === "braces" ||
          parsed.join === "parentheses" ||
          parsed.join === "braces"
        ? true
        : parsed.leading;
  return formatSegment({ ...parsed, join, leading });
}
export function toggleSegment(
  template: string,
  medium: Medium,
  key: keyof NamingChoices,
) {
  const token =
    key === "year"
      ? medium === "audio"
        ? "recording_year"
        : "edition_year"
      : key === "version"
        ? medium === "audio"
          ? "narrator"
          : "edition"
        : key;
  const tokens = key === "year" ? YEAR_TOKENS : [token];
  const segments = templateSegments(template);
  if (
    segments.some((segment) =>
      tokens.some((item) => segment.includes(`{${item}}`)),
    )
  )
    return segments
      .filter(
        (segment) => !tokens.some((item) => segment.includes(`{${item}}`)),
      )
      .join("");
  const enabled = folderTemplate(medium, {
    ...simpleChoices,
    author: false,
    [key]: true,
  });
  const added = templateSegments(enabled).filter(
    (segment) => !segment.includes("{title}"),
  );
  const position = ["author", "series", "sequence", "year"].includes(key)
    ? segments.findIndex((segment) => segment.includes("{title}"))
    : segments.length;
  segments.splice(Math.max(0, position), 0, ...added);
  return segments.join("");
}
