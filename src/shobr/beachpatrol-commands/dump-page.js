export default async ({ context }, url, waitMs) => {
  if (!url) {
    throw new Error("usage: dump-page <url>");
  }

  const page = await context.newPage();
  await page.goto(url, { timeout: 60_000 });

  await page.waitForTimeout(parseInt(waitMs || "4000", 10));

  const html = await page.content();
  const title = await page.title();
  const finalUrl = page.url()

  return JSON.stringify(
    { url: finalUrl, title, html },
    null,
    2,
  );
};
