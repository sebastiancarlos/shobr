The resume below renders past its page limit. Shorten it to fit:
- Assume that the resume is very close to fitting, so a small
  amount of content cut should be enough to make it fit. If not, a second pass
  of cutting will be triggered.
- Don't bother much with counting bytes/characters for figuring out if the
  result would fit in one page: The resume will be rendered as a PDF. So, keep
  in mind that non-monospace font rendering and rendered line breaks will be a
  larger factor in page fit than monospace character count. Thereby, use
  reasonable judgement to guess if the new resume is likely to fit in just one
  page. Falling short is acceptable, because a later step will verify page
  fit, and trigger another rewrite if needed.
- Don't bother with making tool calls to figure out how or in which context
  the markdown will be rendered into a PDF. That is not your concern.
- Possible things to cut:
  - The least important bullet items.
  - Some of the wording on the prose, to be tighter.
  - As a LAST RESORT, the earliest employer (but no other ones).

Reply with JSON only:
{{"resume_md": "<complete shortened resume.md>"}}.

--- BEGIN RESUME ---
{overflowing_md}
--- END RESUME ---

--- BEGIN VERIFY ---
{verify_output}
--- END VERIFY ---
