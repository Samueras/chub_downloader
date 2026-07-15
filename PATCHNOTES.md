# Patch Notes

## v1.8.0

### New Features

- **Download All Own Bots**: New button on the main page that downloads every bot owned by your logged-in account, including private and unlisted bots. Uses the authenticated Chub API (requires your session token). Confirms the batch before downloading and reports a summary (downloaded / skipped / failed) at the end.
  - **Skip / Overwrite prompt**: Pre-scans the output directory and asks whether to skip already-downloaded cards or overwrite them. Respects the current bundle option (folder or zip).
  - **Session token guidance**: If no token is set, shows a step-by-step explanation of how to extract the `session` cookie from the browser.

- **Creator ID Lookup (Advanced Search)**: Magnifying-glass button next to the Creator ID field opens a popup where you can enter a creator username and look up their numeric ID via the Chub API. Displays the ID, username, and avatar, with a "Use this ID" button to fill the field.

- **Interactive HTML Gallery**: The generated `{card}_info.html` now includes a clickable thumbnail gallery of all downloaded gallery images, with a full-screen lightbox (prev/next arrows, keyboard navigation with arrow keys and Esc, click-to-close, and an image counter).

### Improvements

- **Search results stay open after download**: Both the regular Search and Advanced Search results windows now remain open after downloading a card, so you can download multiple cards from a single search without re-running it.
- **Advanced Search window is reusable**: The Search button re-enables when the results window closes, so you can refine filters and run another search without reopening the Advanced Search window.
- **Pagination keeps filters**: Page 2+ of search results now correctly preserves all Advanced Search filters (creator_id, sort, topics, etc.) instead of dropping them.
- **Fixed Creator ID filter**: The creator filter was using the wrong API parameter name (`creator` instead of `creator_id`) and silently returned the entire catalog. Now correctly filters to the selected creator.
- **Trending sort fallback**: The Chub API returns 0 results when combining `sort=trending` with a `creator_id` filter. This combination is now detected automatically and falls back to `sort=download_count`.
- **Batch download progress in status bar**: When using "Download All Own Bots", every status line is prefixed with `DL: #/N <name>` showing the current card index, total card count, and first 10 characters of the bot name (`...` appended if truncated). Example: `DL: 3/28 Valerie-yo... Downloading gallery image 2/5...`

### Reliability

- **Rate-limit retry (HTTP 429)**: Gallery fetches and image downloads (both gallery and main card image) now retry automatically on HTTP 429 responses, with exponential backoff (5s, 10s, 20s, 40s, 80s) up to 5 retries. Mirrors the proven retry logic from ForksScanner. Rate-limit waits are surfaced in the status bar.
- **API call throttling**: Added a 1-second delay between consecutive API calls (rating checks and between cards in a batch) to reduce the chance of hitting rate limits and read timeouts in the first place.
- **Batch error resilience**: If a single card fails after exhausting retries, it is recorded in the `failed` list and the batch continues with the next card instead of aborting.
