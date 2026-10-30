---
title: Dynamic Bus Pricing Intelligence
emoji: 🚌
colorFrom: red
colorTo: orange
sdk: docker
app_port: 7860
pinned: false
---

# redBus Competitor Dynamic Pricing & Relational Storage Pipeline

A modular, production-grade market intelligence and dynamic pricing data pipeline for intercity bus operators on redBus (`redbus.in`), built with **Python 3.11**, **Playwright**, and **SQLite**.

---

## 1. Architecture Overview

```
                      +-----------------------------------+
                      |   CLI Orchestrator (main.py)      |
                      +-----------------+-----------------+
                                        |
                 +----------------------+----------------------+
                 |                                             |
                 v                                             v
+----------------------------------+        +----------------------------------+
|   Playwright Scraper Engine      |        |   Relational SQLite DB (db.py)   |
|   (scraper.py)                   |        +-----------------+----------------+
|                                  |                          |
|  - Asset Blocking (.png, .woff2) |                          v
|  - Network API Interceptor       |        +----------------------------------+
|    (/rpw/api/searchResults)      |        |  Schema (3 Normalized Tables):   |
|  - RTC Accordion Auto-Expand     | =====> |  1. cities (Dimension)          |
|  - Auto-Scroll Stabilization     |        |  2. operators (Dimension)       |
|  - DOM Fallback + CRC32 Hash     |        |  3. fare_snapshots (Fact)        |
+----------------------------------+        +-----------------+----------------+
                                                              |
                                                              v
                                            +----------------------------------+
                                            |  Analytics & Dynamic Pricing     |
                                            |  (analytics_queries.sql)         |
                                            +----------------------------------+
```

---

## 2. Normalized SQLite Schema

The database (`bus_pricing_intelligence.db`) implements a normalized schema designed for time-series dynamic pricing and competitor analysis:

### Table 1: `cities` (Dimension)
| Column | Type | Description |
|---|---|---|
| `city_id` | `INTEGER PRIMARY KEY` | Numeric redBus city ID (e.g., `733` for Delhi, `807` for Jaipur) |
| `city_name` | `TEXT NOT NULL` | City name |
| `state` | `TEXT` | State name |
| `country` | `TEXT` | Country (default: `'India'`) |

### Table 2: `operators` (Dimension)
| Column | Type | Description |
|---|---|---|
| `operator_id` | `INTEGER PRIMARY KEY` | redBus operator ID or deterministic CRC32 hash |
| `operator_name` | `TEXT NOT NULL` | Bus operator brand name |
| `is_rtc` | `INTEGER DEFAULT 0` | Flag for Government State Road Transport Corporation (e.g., RSRTC) |
| `is_primo` | `INTEGER DEFAULT 0` | redBus Primo-certified quality badge |
| `max_rating` | `REAL` | Highest observed customer rating |
| `updated_at` | `TEXT NOT NULL` | ISO-8601 timestamp of last update |

### Table 3: `fare_snapshots` (Fact Table - Append-Only)
| Column | Type | Description |
|---|---|---|
| `snapshot_id` | `TEXT PRIMARY KEY` | Composite snapshot key (`timestamp_from_to_serviceId`) |
| `scraped_at` | `TEXT NOT NULL` | ISO-8601 scraping timestamp |
| `hours_to_departure` | `REAL` | Time remaining until bus departure |
| `from_city_id` | `INTEGER NOT NULL` | Foreign key to `cities.city_id` |
| `to_city_id` | `INTEGER NOT NULL` | Foreign key to `cities.city_id` |
| `travel_date` | `TEXT NOT NULL` | Date of journey (`DD-MMM-YYYY`) |
| `operator_id` | `INTEGER NOT NULL` | Foreign key to `operators.operator_id` |
| `operator_name` | `TEXT NOT NULL` | Operator name |
| `service_id` | `TEXT NOT NULL` | redBus internal service ID |
| `bus_type` | `TEXT` | Coach specification (e.g., `Bharat Benz A/C Seater / Sleeper (2+1)`) |
| `is_ac` | `INTEGER DEFAULT 0` | 1 if Air Conditioned, 0 otherwise |
| `is_sleeper` | `INTEGER DEFAULT 0` | 1 if Sleeper coach, 0 otherwise |
| `departure_time` | `TEXT` | Departure time (`HH:MM:SS`) |
| `arrival_time` | `TEXT` | Arrival time (`HH:MM:SS`) |
| `duration` | `TEXT` | Journey duration (e.g., `06h 40m`) |
| `original_fare` | `REAL` | Base fare before promotional discount |
| `effective_fare` | `REAL` | Final dynamic fare payable |
| `fare_tiers_json` | `TEXT` | JSON list of seat category fare tiers |
| `discount_pct` | `REAL` | Deal discount percentage |
| `total_seats` | `INTEGER` | Total coach seat capacity |
| `seats_available` | `INTEGER` | Remaining inventory (load factor numerator) |
| `single_seats_available` | `INTEGER` | Unreserved single seats |
| `rating` | `REAL` | Operator customer rating |
| `review_count` | `INTEGER` | Total user reviews |
| `boarding_points_json` | `TEXT` | JSON array of boarding locations and departure timestamps |
| `dropping_points_json` | `TEXT` | JSON array of drop-off points |
| `deal_tag` | `TEXT` | Marketing tag (e.g., `REDDEAL`, `Get 8 % Extra OFF`) |

---

## 3. Quick Start & Execution

### Running via Virtual Environment Python
```bash
# Basic single-day scraping run
.\.venv\Scripts\python.exe main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026"

# Multi-day forward booking curve (e.g., today, 1 day ahead, 3 days ahead, 7 days ahead)
.\.venv\Scripts\python.exe main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026" --days-ahead 0,1,3,7

# Filter by a specific operator name
.\.venv\Scripts\python.exe main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026" --operator-name "Zingbus"
```

### Or using the workspace `python.bat` wrapper
```bash
.\python.bat main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026"
```

---

## 4. Key Engineering Highlights

1. **Protocol Resiliency & Akamai Bypass**: Uses a Playwright headless browser configuration to bypass Akamai Bot Manager HTTP/2 stream resets (`ERR_HTTP2_PROTOCOL_ERROR`) that fail plain HTTP requests.
2. **Network Response Interception**: Captures live JSON payloads from redBus's internal `/rpw/api/searchResults` endpoint, capturing complete seat availability, metadata, RTC group IDs, and promotional discount campaigns.
3. **Session-Authenticated In-Page Pagination**: Leverages the live browser session to fetch dense route inventories up to `totalCount`, extracting 200+ bus services per search in seconds.
4. **Static Asset Blocking**: Intercepts and aborts images, media, and fonts (`.png`, `.jpg`, `.webp`, `.woff2`) via `page.route` to conserve 90%+ bandwidth and maximize scraping speed.
5. **Deterministic Operator Hashing**: Generates consistent 32-bit CRC32 IDs (`zlib.crc32`) if redBus omits a numeric ID for smaller DOM tuples.
6. **Append-Only Time Series Snapshots**: Preserves every historical scraping run for longitudinal pricing volatility, load-factor elasticity, and departure-window surge analysis.

---

## 5. Streamlit Market Intelligence Dashboard (`app.py`)

A full-featured web dashboard providing competitor analytics, route exploration, and Excel reporting:

```bash
# Launch the Streamlit dashboard
.\.venv\Scripts\streamlit.exe run app.py

# Or double-click the Windows batch launcher
.\run_dashboard.bat
```

### Dashboard Tabs & Features:
1. **🔍 Multi-Operator Route & Date Explorer**:
   - Sidebar filters for Origin, Destination, Travel Date (DOJ), and dynamic multi-select competitor filtering.
   - 5 KPI metric cards: Total Matching Buses, Lowest Dynamic Fare, Median Dynamic Fare, Average Rating, and Remaining Seat Capacity.
   - Interactive table with category badges (RTC vs. Private, Primo), departure/arrival times, effective fares, discounts, and ratings.
2. **⏱️ 24×7 3-Hour Dynamic Price Matrix (NEW)**:
   - **Continuous 24-Hour Departure Window Breakdown**: Segmented into 8 three-hour blocks (`00-03`, `03-06`, `06-09`, `09-12`, `12-15`, `15-18`, `18-21`, `21-24`).
   - **Executive Window Cards**: Instant badges identifying the *Best Value Window* (lowest median fare) and *Peak Surge Window* (highest dynamic fare).
   - **Competitor Pricing Heatmap (Plotly)**: Visual matrix showing median dynamic fares across operators and 3-hour windows, revealing competitor service gaps.
   - **24-Hour Price Curve & Volume**: Continuous fare spread band (min, median, peak surge) plotted against departure frequencies.
   - **Operator Benchmark Pivot Matrix**: Cross-tabulation of all operators across all 8 three-hour windows.
   - **Service-by-Service Deep Dive**: Head-to-head comparison of every bus service within any 3-hour window with 5 sorting dimensions and dedicated CSV export.
   - **Multi-Snapshot Volatility Tracker**: Longitudinal surge and discount monitoring tracking how individual service fares change across recurring scrape runs.
3. **📊 Competitor Dynamic Pricing Analytics**:
   - **Fare Distribution (Box Plot)**: Side-by-side competitor dynamic pricing ranges (min, median, peak surge).
   - **Yield Management Scatter Plot**: Scatter correlation between remaining seat inventory and effective fare.
   - **Time-of-Day Curve**: Departure hour vs. median dynamic ticket price.
   - **Coach Class Matrix**: Pricing comparisons across AC Sleeper, AC Seater, Non-AC Sleeper, Non-AC Seater.
   - **Discount Aggressiveness Leaderboard**: Ranking operators by average price cuts.
4. **🏙️ Master Entity Directory**:
   - Searchable directories for Cities (with numeric redBus IDs) and Operators (ratings, review counts, Primo status).
   - Interactive forms to insert or update new cities or operators with real-time SQLite upserts.
5. **📥 Excel Export Center**:
   - Generates professionally styled, multi-sheet Excel reports (`.xlsx`) via `openpyxl`.
   - Includes `Executive Summary`, `Operator Benchmarking`, and `All Bus Services` with autofilters, currency formatting, and column auto-width.

---

## 6. Automated Time-Series Scraper Worker (`worker.py`)

Runs as a resilient background worker executing automated route scraping 4 times daily (08:00, 14:00, 19:00, and 22:00 IST) across forward booking windows (`D+0`, `D+1`, `D+3`, `D+7`):

```bash
# Run one complete batch scrape pass immediately across all watchlist routes:
.\.venv\Scripts\python.exe worker.py --run-now

# Run continuously as background daemon at scheduled IST hours:
.\.venv\Scripts\python.exe worker.py --daemon

# Run on a custom recurring interval (e.g. every 6 hours):
.\.venv\Scripts\python.exe worker.py --daemon --interval-hours 6.0
```

- **Watch-list Configuration (`routes_watchlist.json`)**: Configures origin/destination routes, active flags, and forward offsets.
- **Tenacity Resilience**: Auto-retries failed browser requests with exponential backoff (`stop_after_attempt(3)`, `wait_exponential`).
- **Post-Scrape Pipeline Integration**: Automatically executes competitor price drop/surge alerts and refreshes yield recommendations upon ingestion.

---

## 7. Algorithmic Dynamic Pricing & Yield Engine (`pricing_engine.py`)

A revenue management engine that computes optimal dynamic pricing recommendations for bus operators based on real-time redBus market dynamics:

```bash
# Run pricing recommendations for Delhi -> Jaipur:
.\.venv\Scripts\python.exe pricing_engine.py --from-id 733 --to-id 807 --date "27-Oct-2026"

# Evaluate and persist recommendations into SQLite table 'pricing_recommendations':
.\.venv\Scripts\python.exe pricing_engine.py --from-id 733 --to-id 807 --date "27-Oct-2026" --save-db
```

### Yield Algorithms:
1. **Competitor Benchmarking**: Computes median (50th percentile) and 75th percentile effective fares of competitors departing within $\pm 90$ minutes in the same coach tier.
2. **Occupancy Multiplier**:
   - `DISTRESS_DISCOUNT` (0.85x / -15%): Unsold inventory with $< 30\%$ occupancy inside 6 hours of departure.
   - `EARLY_BIRD_INCENTIVE` (0.92x / -8%): Slow booking velocity with $< 40\%$ occupancy $> 24$ hours ahead.
   - `VALUE_OFF_PEAK` (0.95x / -5%): Sub-optimal pace ($< 40\%$) inside 24 hours.
   - `MARKET_PARITY` (1.00x): Balanced load factor (40% - 65%) pegged to market median.
   - `HIGH_DEMAND_SURGE` (1.15x / +15%): High booking momentum (65% - 85%).
   - `SCARCITY_SURGE` (1.35x / +35%): Critical low seat availability ($> 85\%$ booked).
3. **Berth Tier Segmentation**:
   - **Standard / Upper Deck Berth**: `1.00x`
   - **Lower Deck Berth**: `1.12x` (+12% accessibility & ride comfort premium)
   - **Single Sleeper Berth**: `1.22x` (+22% solitude & privacy premium)

---

## 8. Competitor Price Alerting & Webhooks (`alerts.py`)

Monitors longitudinal `fare_snapshots` to detect market price disruptions and dispatches structured alerts via **Slack Webhook**, **Telegram Bot**, and **SQLite**:

```bash
# Run price anomaly & surge evaluation across latest snapshots:
.\.venv\Scripts\python.exe alerts.py --check

# Test Slack / Telegram webhook connectivity with a simulated test alert:
.\.venv\Scripts\python.exe alerts.py --test-alert
```

### Alert Triggers:
- **Price Cut Alert (`COMPETITOR_PRICE_DROP`)**: Competitor slashes dynamic fare by $\ge 10\%$ vs. their previous snapshot.
- **Last-Minute Surge Alert (`LAST_MINUTE_SURGE`)**: Competitor increases fares inside 24 hours of departure.
- **Inventory Exhaustion Alert (`LOW_INVENTORY_SURGE`)**: Competitor drops to $\le 3$ remaining seats within 12 hours of departure.
- **Channels**: Dispatches to `SLACK_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, and stores history in the `price_alerts` SQLite table.
