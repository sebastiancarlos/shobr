export default async ({ context }, url, waitMs) => {
  const NOTIFICATIONS_URL = "https://www.linkedin.com/notifications";
  const page = await context.newPage();
  await page.goto(url || NOTIFICATIONS_URL, { timeout: 60_000 });

  // Give client-side rendering a moment to settle.
  await page.waitForTimeout(parseInt(waitMs || "2500", 10));

  const rows = await page.evaluate(() => {
    const ROW_MIN_TEXT_LENGTH = 10;
    const buttons = Array.from(document.querySelectorAll('button[aria-label="More options"]'));
    const seen = new Set();
    const rows = [];

    for (const button of buttons) {
      // The row is the smallest ancestor <div> whose text is real content,
      // not just the relative timestamp (the "More options" button is nested
      // a few levels deep inside divs that only hold the time).
      let row = button.parentElement;
      while (row) {
        const text = (row.innerText || "").replace(/\s+/g, " ").trim();
        if (row.tagName === "DIV" && text.length > ROW_MIN_TEXT_LENGTH) break;
        row = row.parentElement;
      }
      if (!row || row === document.body || seen.has(row)) continue;

      const avatar = row.querySelector('img[alt="View profile"], svg[aria-label="View profile"]');
      const actorUrl = avatar && avatar.closest("a[href]") ? avatar.closest("a[href]").href : "";

      const text = (row.innerText || "").replace(/\s+/g, " ").trim();
      const timeMatch = text.match(/\d+[smhdw]\b/g);
      const time = timeMatch ? timeMatch[timeMatch.length - 1] : "";

      // Identity text with the relative-time tokens stripped, so a row that
      // renders "10h" one run and "1d" the next keeps the same content key.
      const plainText = text.replace(/\d+[smhdw]\b/g, "").replace(/\s+/g, " ").trim();

      const links = Array.from(row.querySelectorAll("a[href]"))
        .map((a) => ({ text: (a.innerText || "").replace(/\s+/g, " ").trim(), url: a.href }))
        .filter((link) => link.text.length > 0);

      seen.add(row);
      rows.push({ actor_url: actorUrl, time, links, text, plain_text: plainText });
    }

    return rows;
  });

  return JSON.stringify({ rows }, null, 2);
};
