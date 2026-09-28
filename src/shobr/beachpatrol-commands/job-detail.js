export default async ({ context }, url) => {
  if (!url) {
    throw new Error("usage: job-detail <url>");
  }

  const page = await context.newPage();
  await page.goto(url, { timeout: 60_000 });

  // wait for page readiness by waiting for "job details" id to appear
  await page.waitForSelector('[id^="JobDetails_AboutTheJob_"]', { timeout: 10_000 });

  const extract = () =>
    page.evaluate(() => {
      // Content sections keep their rich text (paragraphs, bold, lists) when
      // captured as HTML
      const tidyContent = (root) => {
        for (const el of root.querySelectorAll(
          "li, p, strong, b, em, i, a, ul, ol, h1, h2, h3, h4, h5, h6"
        )) {
          while (el.firstElementChild?.tagName === "BR") el.firstElementChild.remove();
          while (el.lastElementChild?.tagName === "BR") el.lastElementChild.remove();
        }
      };
      for (const btn of document.querySelectorAll('[data-testid="expandable-text-button"]')) {
        btn.remove();
      }

      // parse job description
      const jobSection = document.querySelector('[id^="JobDetails_AboutTheJob_"]');
      let job_description_html = "";
      if (jobSection) {
        const sectionHeading = jobSection.querySelector("h2");
        if (sectionHeading && /about the job/i.test(sectionHeading.textContent || "")) {
          sectionHeading.remove();
        }
        tidyContent(jobSection);
        job_description_html = jobSection.innerHTML;
      }

      // parse company description
      const textBoxes = [...document.querySelectorAll('[data-testid="expandable-text-box"]')];
      const companyHeading = [...document.querySelectorAll("h2")].find((h) =>
        (h.textContent || "").trim().toLowerCase().includes("about the company")
      );
      let company_description_html = "";
      if (companyHeading) {
        const isAfter = (box) =>
          (companyHeading.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
        if (!jobSection) {
          job_description_html = textBoxes
            .filter((box) => !isAfter(box))
            .filter(Boolean)
            .map((box) => {
              tidyContent(box);
              return box.innerHTML;
            })
            .join("\n");
        }
        const companyBox = textBoxes.find(isAfter);
        if (companyBox) {
          tidyContent(companyBox);
          company_description_html = companyBox.innerHTML;
        }
      } else if (!jobSection) {
        job_description_html = textBoxes
          .filter(Boolean)
          .map((box) => {
            tidyContent(box);
            return box.innerHTML;
          })
          .join("\n");
      }

      // extract more info (pills): discover via the check-small
      // design-system icons beside pill labels (stable, unlike the hashed
      // layout classes), then validate. Salary pills carry no icon, so they
      // fall back to a scoped text scan. Both skip description boxes.
      const pillLabels = [];
      const seenPills = new Set();
      const pushLabel = (el) => {
        const text = (el.textContent || "").trim();
        if (!text || seenPills.has(text)) return;
        if (el.closest('[data-testid="expandable-text-box"]')) return;
        seenPills.add(text);
        pillLabels.push(text);
      };
      for (const icon of document.querySelectorAll('[id="check-small"]')) {
        let sib = icon.nextElementSibling;
        while (sib && sib.tagName !== "SPAN") sib = sib.nextElementSibling;
        if (sib) pushLabel(sib);
      }
      for (const el of document.querySelectorAll("span")) {
        if (/^\$/.test((el.textContent || "").trim())) pushLabel(el);
      }
      const location_type =
        pillLabels.find((p) => ["Remote", "Hybrid", "On-site"].includes(p)) || null;
      const employment_type =
        pillLabels.find((p) =>
          ["Full-time", "Part-time", "Contract", "Temporary", "Internship"].includes(p)
        ) || null;
      const salary_range = pillLabels.find((p) => /^\$/.test(p)) || null;

      // get application info (match known message variants)
      const CLOSED_MESSAGE = /(No longer|Not currently) accepting applications/i;
      const outsideDescriptions = (el) => !el.closest('[data-testid="expandable-text-box"]');
      const closedBanner = [...document.querySelectorAll('[aria-atomic="true"]')]
        .filter(outsideDescriptions)
        .find((el) => CLOSED_MESSAGE.test(el.textContent || ""));
      const closedNotice = [...document.querySelectorAll('[id="signal-notice-small"]')].find(
        (icon) => CLOSED_MESSAGE.test(icon.parentElement?.textContent || "")
      );
      const accepting_applications = !closedBanner && !closedNotice;
      let apply_method = null;
      let apply_url = null;
      if (accepting_applications) {
        const external = document.querySelector('a[aria-label="Apply on company website"]');
        if (external) {
          apply_method = "external";
          const href = external.getAttribute("href") || "";
          try {
            const u = new URL(href, window.location.href);
            const target = u.searchParams.get("url");
            apply_url = target ? decodeURIComponent(target) : href;
          } catch {
            apply_url = href;
          }
        } else if (
          document.querySelector(
            '[data-easy-apply-apply-button], a[aria-label*="Easy Apply"], button[aria-label*="Easy Apply"]'
          )
        ) {
          apply_method = "easy_apply";
        }
      }

      return {
        job_description_html,
        company_description_html,
        location_type,
        employment_type,
        salary_range,
        accepting_applications,
        apply_method,
        apply_url,
      };
    });

  const detail = await extract();

  return JSON.stringify(detail, null, 2);
};
