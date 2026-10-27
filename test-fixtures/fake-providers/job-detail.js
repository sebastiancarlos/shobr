export default async ({ context }, url) => {
  const page = await context.newPage();
  await page.goto(url, { timeout: 60_000 });
  const raw_html = await page.content();
  const detail = await page.evaluate(() => {
    const apply = document.querySelector("#fake-apply a");
    return {
      title: document.querySelector("#fake-title")?.textContent?.trim() || "",
      job_description_html: document.querySelector("#fake-description")?.innerHTML || "",
      company_description_html: "",
      location_type: null,
      employment_type: null,
      salary_range: null,
      accepting_applications: true,
      apply_method: apply ? "external" : null,
      apply_url: apply?.href || null,
    };
  });
  return JSON.stringify({ ...detail, raw_html }, null, 2);
};
