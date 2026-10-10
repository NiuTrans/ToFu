/** Exact-source LRU cache, shared capacity across raw/escaped variants.
 * Entry point: createMarkdownRenderCache. Dependencies: none.
 * Source strings are native Map keys: no sampled hashes or prefixed copies.
 */
interface Entry { readonly source: string; readonly escaped: boolean; readonly html: string }

export function createMarkdownRenderCache(maximumEntries: number) {
  if (!Number.isSafeInteger(maximumEntries) || maximumEntries < 0) {
    throw new RangeError('Markdown cache capacity must be a non-negative safe integer');
  }
  const variants = [new Map<string, Entry>(), new Map<string, Entry>()];
  const recency = new Map<Entry, true>();
  let retainedCharacters = 0;
  const remove = (entry: Entry) => {
    variants[Number(entry.escaped)].delete(entry.source);
    recency.delete(entry);
    retainedCharacters -= entry.source.length + entry.html.length;
  };
  return {
    get(source: string, escaped: boolean): string | undefined {
      const entry = variants[Number(escaped)].get(source);
      if (!entry) return undefined;
      recency.delete(entry); recency.set(entry, true);
      return entry.html;
    },
    set(source: string, escaped: boolean, html: string): void {
      if (maximumEntries === 0) return;
      const previous = variants[Number(escaped)].get(source);
      if (previous) remove(previous);
      const entry = { source, escaped, html };
      variants[Number(escaped)].set(source, entry); recency.set(entry, true);
      retainedCharacters += source.length + html.length;
      while (recency.size > maximumEntries) remove(recency.keys().next().value!);
    },
    clear(): void {
      variants.forEach(cache => cache.clear()); recency.clear(); retainedCharacters = 0;
    },
    stats() { return { entries: recency.size, retainedCharacters }; },
  };
}
