// Fetch at most this many result pages per discovery run. Each page has 25
// cards, so this caps a run at roughly 100 postings. Raise it if you want more.
const MAX_PAGES = 4;

// Parses the cards on the current page. Runs in the page context, so it must
// not close over module-level values.
const parseCards = () => {
  const PREFIX = "job-card-component-ref-";
  const seen = new Set();
  const rows = [];

  for (const card of document.querySelectorAll(`[componentkey^="${PREFIX}"]`)) {
    // componentkey appears on both the clickable card and a nested div;
    // only keep the first (outermost) occurrence per posting.
    const id = card.getAttribute("componentkey").replace(PREFIX, "");
    if (seen.has(id)) continue;
    seen.add(id);

    // The card is a flat text column: title first, then company, then
    // location. When a "(Verified job)" badge is rendered, the title is
    // repeated on the next line, so skip a duplicate before indexing.
    const lines = (card.innerText || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);

    const stripBadge = (s) => s.replace(/\s*\(Verified job\)\s*$/, "");
    const title = stripBadge(lines[0] || "");
    let rest = lines.slice(1).map(stripBadge);
    if (rest[0] === title) rest = rest.slice(1);

    rows.push({
      posting_id: id,
      posting_url: `https://www.linkedin.com/jobs/view/${id}`,
      title,
      company: rest[0] || "",
      location: rest[1] || "",
    });
  }

  return rows;
};

// Pagination renders numbered buttons plus a trailing Next chevron as plain
// buttons (no hrefs to follow), so the loop clicks one of them and waits for
// the results to re-render in place. The Visible/Hidden state is spelled into
// the testid: a hidden chevron means we are on the last page.
const nextPageSelector = () => {
  if (document.querySelector('[data-testid="pagination-controls-next-button-visible"]')) {
    return '[data-testid="pagination-controls-next-button-visible"]';
  }
  if (document.querySelector('[data-testid="pagination-controls-next-button-hidden"]')) return null;

  // No chevron at all: fall back to the numbered bar, clicking the page after
  // the one marked current.
  const items = document.querySelectorAll('[data-testid="pagination-controls-list"] li');
  for (let i = 0; i < items.length; i += 1) {
    if (items[i].querySelector('[aria-current="true"]')) {
      return i + 1 < items.length
        ? `[data-testid="pagination-controls-list"] li:nth-child(${i + 2}) button`
        : null;
    }
  }
  return null;
};

export default async ({ context }, url, waitMs) => {
  if (!url) {
    throw new Error("usage: linkedin-job-search <url>");
  }

  const wait = parseInt(waitMs || "4000", 10);
  const page = await context.newPage();
  const rowsById = new Map();
  let firstHtml = "";
  let pages = 0;

  try {
    // Failure here abandons the whole run (no pages parsed yet, nothing to
    // return); later click failures keep the pages already collected.
    await page.goto(url, { timeout: 60_000 });
    while (pages < MAX_PAGES) {
      await page.waitForTimeout(wait);
      if (pages === 0) firstHtml = await page.content();

      const pageRows = await page.evaluate(parseCards);
      pages += 1;
      if (pageRows.length === 0) break;

      const before = rowsById.size;
      for (const row of pageRows) rowsById.set(row.posting_id, row);

      // If the parse yields no new ids, the click did not truly advance the
      // results, and treating that as the end stops the loop from spinning.
      if (rowsById.size === before) break;

      const selector = await page.evaluate(nextPageSelector);
      if (!selector) break;

      // Pace between pages the way a human would pause before stepping to the
      // next result page, with a little jitter. A zero wait (the test suite
      // passes 0 to skip all artificial delays) skips the pause too.
      await page.waitForTimeout(wait > 0 ? 6_000 + Math.random() * 4_000 : 0);
      await page.click(selector, { timeout: 15_000 });

      // After a click the list re-renders in place, so the pagination marker
      // can advance while the cards are still ghosted and hydrating. Gate on
      // the first card then: it must carry real content and an id that differs 
      // from the previous page's first card. Runs in the page context.
      const prevFirstId = pageRows[0]?.posting_id || "";
      try {
        await page.waitForFunction(
          (prev) => {
            const first = document.querySelector('[componentkey^="job-card-component-ref-"]');
            // No first card yet, or the list still shows the previous page's
            // first card: keep waiting.
            if (!first) return false;
            const id = first.getAttribute("componentkey").replace("job-card-component-ref-", "");
            if (id === prev) return false;
            // Real content, not a skeleton: a nonempty text column.
            return Boolean((first.innerText || "").trim());
          },
          prevFirstId,
          { timeout: 60_000 },
        );
      } catch (error) {
        // A first-card that never turns over keeps the pages we already have.
        console.error(`linkedin-job-search: stopped after ${pages} page(s): ${error.message}`);
        break;
      }
    }
  } finally {
    await page.close().catch(() => {});
  }

  return JSON.stringify({ rows: [...rowsById.values()], raw_html: firstHtml }, null, 2);
};
