# Nintendo Switch 2 Price Tracker (Indonesia)

A GitHub‑based price tracker for Nintendo Switch 2 listings on Tokopedia, Shopee, and Blibli.  
It scrapes prices every 30 minutes and displays them on a static dashboard.

## How to Use

1. Fork this repository.
2. Enable **GitHub Pages** in your fork:
   - Go to `Settings` → `Pages`.
   - Source: `Deploy from a branch`, branch `main`, folder `/` (root).
   - Save. Your dashboard will be available at `https://<your-username>.github.io/<repo-name>/`.
3. That’s it! The GitHub Actions workflow will run automatically and update `data.json` every 30 minutes.
4. Visit your dashboard URL to see live prices and historical charts.

## Manual Run

You can manually trigger the scraper from the `Actions` tab → `Scrape Prices` → `Run workflow`.

## Customisation

- Edit the `PRODUCTS` list in `scrape.py` to add/remove tracking URLs.
- Adjust the cron schedule in `.github/workflows/scrape.yml` (e.g., change `*/30 * * * *` to `0 */6 * * *` for every 6 hours).

## Disclaimer

This project is for educational purposes only. Web scraping may violate the terms of service of the target websites. Always respect `robots.txt` and avoid excessive requests.