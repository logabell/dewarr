// Mirrors parse_title_labels in services/app/domain/catalog_titles.py closely
// enough to prefill a search box and a part number. The server stays the
// authority on what a title means.
const PART =
  /\s*[([]\s*(?:part\s+)?(\d{1,2})\s+of\s+(\d{1,2})\s*[)\]]|\s+(\d{1,2})\s+of\s+(\d{1,2})$/i;
const LABEL =
  /\s*[([]\s*(?:dramati[sz]ed(?:\s+adaptation)?|full[\s-]cast(?:\s+edition)?|(?:un)?abridged)\s*[)\]]|:\s*dramati[sz]ed adaptation$/i;

export function titlePart(title: string): [number, number] | null {
  const match = PART.exec(title);
  if (!match) return null;
  const part = Number(match[1] ?? match[3]);
  const total = Number(match[2] ?? match[4]);
  return part >= 1 && part <= total && total >= 2 && total <= 20
    ? [part, total]
    : null;
}

/** The title to search a catalog with: without part numbers and recording labels. */
export function searchTitle(title: string): string {
  let value = title.trim();
  for (let previous = ""; previous !== value;) {
    previous = value;
    const next = value.replace(PART, "").replace(LABEL, "").trim();
    if (next) value = next;
  }
  return value;
}
