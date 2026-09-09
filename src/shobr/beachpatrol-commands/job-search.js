export default async ({ context }, url, waitMs) => {
  if (!url) {
    throw new Error("usage: job-search <url>");
  }

  const page = await context.newPage();
  await page.goto(url, { timeout: 60_000 });

  await page.waitForTimeout(parseInt(waitMs || "4000", 10));

  const rows = await page.evaluate(() => {
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
  });

  return JSON.stringify({ rows }, null, 2);
};
