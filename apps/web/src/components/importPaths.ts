// Plans namespace formats; the importer places each one inside its configured library root.
export function libraryRelativePath(path: string | null | undefined): string {
  return (path || "").replace(/^(?:audiobooks|ebooks)\//, "");
}
