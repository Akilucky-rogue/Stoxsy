# Draft: asking AMFI for written permission (for the owner to review, edit and send; nothing has been sent)

Why: AMFI's terms of use allow personal, non-commercial use of the site's data and, as we read them, do not allow automated access or bulk electronic storage unless AMFI agrees in writing. `stoxsy fetch-ter` makes one request a month for the same workbook the page's "Download Excel" button gives. A written yes (or an official data feed) removes the doubt. Use the contact route on amfiindia.com ("Contact Us"); the address is not filled in because we do not know the right department.

Check before sending: replace the bracketed parts; keep it short; keep a copy of the reply with the project documents.

---

**To:** [AMFI contact address / contact form: Contact Us on amfiindia.com]
**Subject:** Request for written permission: monthly download of the "TER of MF Schemes" workbook for personal research use

Dear Sir or Madam,

I am an individual investor writing to ask for AMFI's written permission for a small, specific use of the "Total Expense Ratio (TER) of Mutual Fund Schemes" data on amfiindia.com.

**What I do today.** Once a month I download the workbook for the latest month using the page's own "Download Excel" button and keep it on my own computer, to see the expense ratio of the funds I am considering or already hold, next to their returns.

**What I would like to do.** Fetch that same monthly workbook with a small script I run myself, once a month (at most a few requests in a month, with a pause between them, identified as a personal script, no logins or cookies, stopping at once if the site answers with an error). The script would save the file exactly as AMFI publishes it. I would not change the figures.

**How the data would be used.** For my own personal, non-commercial research only. I would not sell, redistribute, publish or share the data or the files, and I would show AMFI as the source wherever the figures appear in my own notes.

**What I would like from AMFI.**
1. Written confirmation that this monthly use is acceptable, or the conditions under which it would be.
2. If AMFI offers an official data file, feed or API for the TER disclosures, details of how to access it, which I would prefer to use instead.
3. Any limits on frequency, storage or use that I should follow.

I am happy to stop or adjust at your request. Thank you for your time.

Yours faithfully,
[Your name]
[Your email address]
[Optional: phone number]

---

Notes for you, not part of the email:
- If AMFI says yes in writing: save the reply in `docs/` (names redacted if you prefer) and we remove the "permission open" notes from the tracker.
- If AMFI says no, or asks us to stop: we stop using `fetch-ter` and go back to saving the file by hand once a month (the loader works the same either way).
- If AMFI offers a feed: we build that reader in place of the page download; the loader, ledger and Funds page do not change.
