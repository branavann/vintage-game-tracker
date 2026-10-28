# Vintage Game Sales

Sold prices for complete-in-box early prints of **Pocket Monsters Red** and **Green** (large barcode) and **Super Mario Bros.** (no barcode, 5000-point misprint manual), from Yahoo! Auctions, Yahoo!フリマ and Mercari. Each game has a toggle between copies with and without the survey postcard / errata sheet.

- `index.html`, `assets/` — the website (GitHub Pages)
- `data/sales.json` — every sale, refreshed every 6 hours by GitHub Actions
- `data/overrides.json` — listings you've hidden (✕) or confirmed (✓) on the site
- `scraper/tracker.py` — the scraper; `.github/workflows/scrape.yml` runs it

## One-time setup

**1. Put it on GitHub (VS Code).** File → Open Folder → this folder. Click the Source Control icon in the left bar → **Publish to GitHub** → sign in → choose **Publish to GitHub public repository**. Include all files when asked.

**2. Turn on the website.** On github.com, open the repo → **Settings → Pages**. Under *Build and deployment*: Source **Deploy from a branch**, Branch **main**, folder **/ (root)** → Save. About a minute later the site is at `https://YOUR-USERNAME.github.io/REPO-NAME/`.

**3. Start the scraper.** Repo → **Actions** tab (click *enable workflows* if GitHub asks) → **Scrape sales** → **Run workflow**. It takes 5–10 minutes. Open the finished run: the summary shows how many sales came from each marketplace, and a yellow warning appears if Yahoo or Mercari returned nothing. After that it runs by itself every 6 hours.

**4. Turn on owner mode (✕ hide / ✓ confirm).** On the site, click the lock icon and follow the steps. The token is a fine-grained GitHub token limited to this one repo with *Contents: Read and write*. Do it once per device. Visitors never see the buttons.

## How listings are sorted

Only complete-in-box copies of the exact print are charted. Each listing lands in one of three places:

- **Charted** — box mentioned and the print confirmed by the title (Pokémon: バーコード大 or a postcard; Mario: no-barcode box + 5000-point misprint, or an errata sheet).
- **Needs review** — probably right but unproven: no box mentioned, barcode size not stated, or a price far outside the chart (under 20% of the typical sale, or a "without postcard" copy priced like one with it). Shown under the *Needs review* button, not charted. ✓ charts it.
- **Not stored** — loose carts, manual-only, sealed, graded, lots, other versions, later prints.

Mercari doesn't show a sold date. The scraper uses the time the listing last changed, which is the sale or shortly after, and the site marks those dates with ≈.

## If Mercari or Yahoo comes back empty on GitHub

Some Japanese sites block cloud servers. If the Actions run warns that Mercari returned 0, run the scraper from your Mac instead and push the result:

```
pip install -r scraper/requirements.txt
python -m playwright install chromium
python scraper/tracker.py
```

Then in VS Code: Source Control → commit → **Sync Changes**. Yahoo can keep running on GitHub; add `--no-yahoo` locally to only do Mercari.

## Other things

- **Preview locally:** in this folder run `python3 -m http.server`, then open http://localhost:8000.
- **A sale the search missed:** `python scraper/tracker.py --add "https://jp.mercari.com/item/m…"`.
- **Add a game:** add an entry to `TITLES` in `scraper/tracker.py` (search terms, title patterns, variants). The site picks it up automatically.
- GitHub pauses scheduled runs if a repo has no activity for 60 days; the scraper's own commits count, so this only happens if sales stop changing. Re-enable from the Actions tab.
