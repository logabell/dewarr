export type Inline =
  | { type: "text"; value: string }
  | { type: "code"; value: string }
  | { type: "strong"; value: string }
  | { type: "link"; text: string; href: string };

export type NoteBlock =
  | { type: "heading"; level: 1 | 2 | 3; inlines: Inline[] }
  | { type: "paragraph"; inlines: Inline[] }
  | { type: "list"; ordered: boolean; items: Inline[][] }
  | { type: "code"; value: string };

const LINK = /\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,2000})\)/;
const CODE = /`([^`\n]+)`/;
const STRONG = /\*\*([^*\n]+)\*\*/;

function earliest(source: string) {
  const found = [
    { type: "link" as const, match: LINK.exec(source) },
    { type: "code" as const, match: CODE.exec(source) },
    { type: "strong" as const, match: STRONG.exec(source) },
  ].filter((item) => item.match?.index !== undefined);
  found.sort((left, right) => left.match!.index - right.match!.index);
  return found[0];
}

export function noteInlines(source: string): Inline[] {
  const inlines: Inline[] = [];
  let rest = source;
  while (rest) {
    const next = earliest(rest);
    if (!next?.match || next.match.index < 0) {
      inlines.push({ type: "text", value: rest });
      break;
    }
    if (next.match.index > 0)
      inlines.push({ type: "text", value: rest.slice(0, next.match.index) });
    if (next.type === "link")
      inlines.push({
        type: "link",
        text: next.match[1],
        href: next.match[2],
      });
    else if (next.type === "code")
      inlines.push({ type: "code", value: next.match[1] });
    else inlines.push({ type: "strong", value: next.match[1] });
    rest = rest.slice(next.match.index + next.match[0].length);
  }
  return inlines;
}

export function releaseNoteBlocks(source: string): NoteBlock[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: NoteBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", value: code.join("\n") });
      continue;
    }
    const heading = /^(#{1,3}) (.+)$/.exec(line);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length as 1 | 2 | 3,
        inlines: noteInlines(heading[2].trim()),
      });
      index += 1;
      continue;
    }
    const marker = /^\s*([-*]|\d{1,3}\.) /;
    if (marker.test(line)) {
      // Items may continue on indented lines, and nested items are flattened into the list.
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (index < lines.length) {
        const current = lines[index];
        if (marker.test(current))
          items.push(current.replace(marker, "").trim());
        else if (current.trim() && /^\s/.test(current) && items.length)
          items[items.length - 1] += ` ${current.trim()}`;
        else break;
        index += 1;
      }
      blocks.push({ type: "list", ordered, items: items.map(noteInlines) });
      continue;
    }
    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !lines[index].startsWith("```") &&
      !/^#{1,3} /.test(lines[index]) &&
      !/^\s*([-*]|\d{1,3}\.) /.test(lines[index])
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({
      type: "paragraph",
      inlines: noteInlines(paragraph.join(" ")),
    });
  }
  return blocks;
}

export function versionLabel(version: string) {
  return version.startsWith("v") ? version : `v${version}`;
}

export function semver(tag: string): [number, number, number] | null {
  const match = /^v?(\d+)\.(\d+)\.(\d+)(?:\+[\w.-]+)?$/.exec(tag);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

export function releaseIsNewer(installed: string, tag: string) {
  const current = semver(installed);
  const next = semver(tag);
  if (!current || !next) return false;
  for (let index = 0; index < 3; index += 1)
    if (next[index] !== current[index]) return next[index] > current[index];
  return false;
}

export function releaseDate(value: string | null) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
