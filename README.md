# 🎮 Nintendo Switch 2 — Price Tracker Indonesia

A full-stack price tracking system for Nintendo Switch 2 across official sellers on **Tokopedia**, **Shopee**, and **BliBli**.

- **Python engine** fetches, parses, and stores prices using free search APIs
- **SQLite database** persists all history across runs (cached between GitHub Actions jobs)
- **GitHub Actions** runs the engine every 6 hours automatically
- **GitHub Pages** serves the dashboard as a static site — no server needed

```
┌─────────────────────────────────────────────────────────┐
│                   GitHub Actions (cron)                 │
│                                                         │
│  engine/main.py                                         │
│       │                                                 │
│       ├─ fetcher.py ──► SerpApi / Serper.dev            │
│       ├─ store.py   ──► ns2_tracker.db (SQLite)         │
│       └─ exporter.py ─► dashboard/data/*.json           │
│                                  │                      │
│                           git commit & push             │
└──────────────────────────────────┼──────────────────────┘
                                   │
                         GitHub Pages serves
                         dashboard/index.html
                         dashboard/data/*.json
```

---

## Repository layout

```
ns2-tracker/
├── .github/
│   └── workflows/
│       └── fetch-prices.yml   ← GitHub Actions cron workflow
├── engine/
│   ├── main.py                ← CLI entry point / orchestrator
│   ├── config.py              ← Settings, seller list, search queries
│   ├── fetcher.py             ← SerpApi + Serper.dev with retry logic
│   ├── store.py               ← SQLAlchemy DB read/write (upsert + history)
│   ├── models.py              ← ORM models: Seller, Listing, PriceHistory, FetchLog
│   ├── exporter.py            ← Writes prices.json / history.json / meta.json
│   ├── requirements.txt
│   └── .env.example
├── dashboard/
│   ├── index.html             ← Single-file dashboard (HTML + CSS + JS)
│   └── data/
│       ├── prices.json        ← Latest listing snapshot  ← written by engine
│       ├── history.json       ← Full price history log   ← written by engine
│       └── meta.json          ← Stats + fetch audit log  ← written by engine
├── .gitignore
└── README.md
```

---

## Setup guide

### Step 1 — Fork and clone the repo

```bash
# On GitHub: click Fork (top-right of this repo)
# Then locally:
git clone https://github.com/YOUR_USERNAME/ns2-tracker.git
cd ns2-tracker
```

---

### Step 2 — Get a free API key

You need **at least one** of the following. Both are free:

#### Option A — SerpApi (100 searches/month free)
1. Go to [serpapi.com](https://serpapi.com) → **Sign up free**
2. After signup → Dashboard → copy your **API Key**
3. Each engine run fires ~4 searches → 100 free credits = ~25 runs/month

#### Option B — Serper.dev (2,500 searches/month free ⭐ recommended)
1. Go to [serper.dev](https://serper.dev) → **Get Started Free**
2. After signup → copy your **API Key**
3. 2,500 credits = ~625 runs/month — comfortably covers 6-hourly runs

> You can set **both** keys. The engine uses SerpApi first and falls back to Serper.dev automatically.

---

### Step 3 — Add secrets to GitHub

1. On GitHub, open your forked repo
2. Go to **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** and add:

| Name | Value | Required |
|------|-------|----------|
| `SERPAPI_KEY` | Your SerpApi key | Optional (one of the two is required) |
| `SERPER_KEY` | Your Serper.dev key | Optional (one of the two is required) |

> **Security note:** Secrets are encrypted and never visible in logs. The engine reads them as environment variables inside the Actions runner — they never touch the dashboard or any committed file.

---

### Step 4 — Enable GitHub Pages

1. In your repo, go to **Settings** → **Pages**
2. Under **Source**, select **Deploy from a branch**
3. Set **Branch** to `main` and folder to `/ (root)`
4. Click **Save**
5. After ~60 seconds your dashboard is live at:
   ```
   https://YOUR_USERNAME.github.io/ns2-tracker/dashboard/
   ```

---

### Step 5 — Trigger the first engine run

The engine runs automatically every 6 hours via cron. To run it immediately:

1. Go to your repo → **Actions** tab
2. Click **Fetch prices** in the left sidebar
3. Click **Run workflow** → **Run workflow** (green button)
4. Watch the logs in real time

After the run completes (~30 seconds), it will commit updated `dashboard/data/*.json` files. Reload your GitHub Pages URL to see live prices.

---

### Step 6 — (Optional) run locally

```bash
cd engine

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
# Edit .env and fill in SERPAPI_KEY and/or SERPER_KEY

# Run once
python main.py

# Check status
python main.py --status

# Run as daemon (fetches every FETCH_INTERVAL_MINUTES)
python main.py --daemon
```

---

## Configuration reference

All config lives in `engine/config.py`. The most useful settings:

| Variable | Default | Description |
|---|---|---|
| `SERPAPI_KEY` | — | SerpApi key (env var / secret) |
| `SERPER_KEY` | — | Serper.dev key (env var / secret) |
| `DATABASE_URL` | `sqlite:///ns2_tracker.db` | SQLAlchemy DB URL |
| `DATA_DIR` | `dashboard/data` | Where JSON files are written |
| `FETCH_INTERVAL_MINUTES` | `60` | Daemon mode interval |

To **add or remove sellers**, edit the `OFFICIAL_SELLERS` list in `engine/config.py`:

```python
OFFICIAL_SELLERS = [
    {"name": "PS Enterprise",  "platform": "Tokopedia", "url": "..."},
    {"name": "YourNewSeller",  "platform": "Shopee",    "url": "..."},
    # ...
]
```

To **change the cron schedule**, edit `.github/workflows/fetch-prices.yml`:

```yaml
schedule:
  - cron: "0 */6 * * *"   # every 6 hours (default)
  - cron: "0 */3 * * *"   # every 3 hours
  - cron: "0 9,21 * * *"  # 9 AM and 9 PM WIB (UTC+7 → subtract 7 = 02, 14 UTC)
```

> **WIB tip:** GitHub Actions uses UTC. To run at 9 AM WIB (UTC+7), use hour `2` in cron.

---

## Manual workflow triggers

From **Actions → Fetch prices → Run workflow**, you can pick:

| Mode | What it does |
|---|---|
| `fetch` (default) | Full fetch → store → export → commit |
| `export-only` | Re-export DB to JSON without fetching (useful after config changes) |
| `status` | Print latest listings and fetch logs, no file changes |

---

## How the engine works

### 1. Fetch (`fetcher.py`)

Fires up to 4 targeted Google searches via SerpApi or Serper.dev, one per platform:

```
Nintendo Switch 2 harga resmi site:tokopedia.com PS Enterprise OR GSShop …
Nintendo Switch 2 harga resmi site:shopee.co.id Drakuli OR Supersonicgamestore …
Nintendo Switch 2 harga resmi site:blibli.com Gamestation OR iBox …
Nintendo Switch 2 Indonesia harga resmi 2025 toko resmi  ← broad fallback
```

Each result snippet is parsed for:
- **Price** — regex extracts IDR amounts (`Rp 6.299.000`, `6299000`, etc.)
- **Platform** — detected from the result URL domain
- **Seller** — fuzzy-matched against the official sellers list
- **Stock** — keyword detection (`habis`, `kosong`, `sisa`, etc.)

### 2. Store (`store.py`)

- **`listings` table** — one row per seller+platform, upserted on every run. Stores `prev_price` so the dashboard can show deltas.
- **`price_history` table** — append-only log, one row per seller per run. Powers the history chart and CSV export.
- **`fetch_logs` table** — audit log for every run: timestamp, source, counts, errors.

### 3. Export (`exporter.py`)

Reads from the DB and writes three JSON files to `dashboard/data/`:

| File | Contents | Used by |
|---|---|---|
| `prices.json` | Latest snapshot per seller | Live price table |
| `history.json` | All history rows (up to 1000) | Chart + history tab |
| `meta.json` | Stats, platform breakdown, fetch logs | Summary cards |

### 4. Commit (GitHub Actions)

The workflow does `git add dashboard/data/*.json && git commit && git push`. GitHub Pages auto-deploys on push, so the dashboard updates within ~1 minute of every engine run.

---

## Database schema

```
sellers          — name, platform, base_url, is_active
listings         — seller+platform (unique), price, prev_price, stock, fetched_at
price_history    — append-only log, recorded_at timestamp
fetch_logs       — one row per run, success/error, duration
```

The SQLite database is **cached between GitHub Actions runs** using `actions/cache`. The cache key rotates weekly, keeping storage bounded. History accumulates correctly across the cache boundary because the engine appends new rows on every run.

---

## Extending to PostgreSQL

For a persistent production database (history never lost between cache rotations):

1. Create a free PostgreSQL instance on [Supabase](https://supabase.com) or [Neon](https://neon.tech)
2. Copy the connection string: `postgresql://user:pass@host:5432/dbname`
3. Add it as a GitHub secret: `DATABASE_URL`
4. The engine auto-detects the URL and uses `psycopg2` instead of SQLite

```bash
# Add to requirements.txt if using PostgreSQL:
psycopg2-binary==2.9.9
```

---

## Troubleshooting

**Dashboard shows "demo data" after first run**
→ The Actions workflow hasn't run yet, or it ran but found 0 listings. Check the Actions log for errors. Trigger a manual run.

**"No listings parsed" in the log**
→ Your API quota may be exhausted. Check your SerpApi/Serper dashboard. Add the other key as a fallback.

**Prices look stale**
→ Check the `meta.json` `updated_at` field. If it's old, the cron may be paused (GitHub pauses Actions on inactive repos after 60 days). Re-enable via Actions → Enable workflows.

**Engine runs but doesn't commit**
→ Confirm the workflow has `permissions: contents: write`. Check that no branch protection rule blocks bot commits.

---

## License

MIT
