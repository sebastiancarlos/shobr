export default async ({ context }, url, waitMs) => {
  const page = await context.newPage();
  await page.goto(url, { timeout: 60_000 });
  await page.waitForTimeout(parseInt(waitMs || "0", 10));
  const raw_html = await page.content();
  const rows = await page.evaluate(() =>
    [...document.querySelectorAll(".fake-card")].map((card) => ({
      posting_id: card.dataset.id,
      posting_url: `${location.origin}/jobs/${card.dataset.id}`,
      title: card.dataset.title,
      company: card.dataset.company || "",
      location: card.dataset.location || "",
    }))
  );
  return JSON.stringify({ rows, raw_html }, null, 2);
};
