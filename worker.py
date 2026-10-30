"""
worker.py - Automated Time-Series Scraper & Yield Intelligence Worker

Role & Purpose:
Runs as a resilient background worker that automatically scrapes target intercity bus routes
at scheduled intervals (08:00, 14:00, 19:00, and 22:00 IST) across forward booking windows
(D+0, D+1, D+3, D+7).

Key Features:
1. Watch-list Configuration: Reads routes & forward windows from 'routes_watchlist.json'.
2. Fault Tolerance: Tenacity exponential backoff with retries across Playwright sessions.
3. Automated Pipeline Integration: Persists snapshots to SQLite, triggers competitor price alerts,
   and refreshes dynamic yield management recommendations.
4. Flexible Execution Modes:
   - Daemon Mode: Continuous scheduler targeting IST hours.
   - Immediate Mode (--run-now): Runs a single complete cycle and exits.
   - Interval Mode (--interval-hours N): Recurring run every N hours.
"""

import sys
import os
import json
import asyncio
import logging
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

from db import (
    init_db,
    upsert_city,
    upsert_operator,
    insert_fare_snapshots,
    DEFAULT_DB_PATH
)
from scraper import RedBusScraper
from alerts import check_and_dispatch_alerts
from pricing_engine import DynamicPricingEngine

# Configure stdout and logging
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logger = logging.getLogger("ScraperWorker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_WATCHLIST_CONFIG = "routes_watchlist.json"
IST_TIMEZONE_OFFSET = timedelta(hours=5, minutes=30)


def get_current_ist_time() -> datetime:
    """Returns the current local time in Indian Standard Time (IST, UTC+5:30)."""
    return datetime.now(timezone.utc) + IST_TIMEZONE_OFFSET


def load_or_create_watchlist(config_path: str = DEFAULT_WATCHLIST_CONFIG) -> Dict[str, Any]:
    """
    Loads routes watchlist JSON file. If missing, writes default configuration
    with major high-density trunk routes (Delhi-Jaipur, Delhi-Agra, Jaipur-Delhi).
    """
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading watchlist from '{config_path}': {e}. Using built-in defaults.")

    default_config = {
        "schedule_times_ist": ["08:00", "14:00", "19:00", "22:00"],
        "forward_booking_windows": [0, 1, 3, 7],
        "routes": [
            {
                "from_id": 733,
                "from_name": "Delhi",
                "to_id": 807,
                "to_name": "Jaipur (Rajasthan)",
                "enabled": True
            },
            {
                "from_id": 733,
                "from_name": "Delhi",
                "to_id": 1290,
                "to_name": "Agra",
                "enabled": True
            },
            {
                "from_id": 807,
                "from_name": "Jaipur (Rajasthan)",
                "to_id": 733,
                "to_name": "Delhi",
                "enabled": True
            }
        ]
    }

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)
        logger.info(f"Generated default watchlist configuration file at '{config_path}'.")
    except Exception as e:
        logger.warning(f"Could not persist default configuration to '{config_path}': {e}")

    return default_config


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)
async def scrape_route_with_retry(
    from_id: int,
    from_name: str,
    to_id: int,
    to_name: str,
    date_of_journey: str,
    db_path: str = DEFAULT_DB_PATH
) -> int:
    """
    Executes resilient route scraping with exponential backoff and retry handling.
    Upserts discovered cities and operators, and inserts append-only fare snapshots.
    """
    logger.info(f"Initiating scrape: {from_name} ({from_id}) ➔ {to_name} ({to_id}) | DOJ: {date_of_journey}")

    # Ensure cities exist in dimension table
    upsert_city(city_id=from_id, city_name=from_name, db_path=db_path)
    upsert_city(city_id=to_id, city_name=to_name, db_path=db_path)

    scraper = RedBusScraper(
        from_city_id=from_id,
        from_city_name=from_name,
        to_city_id=to_id,
        to_city_name=to_name,
        date_of_journey=date_of_journey,
        headless=True
    )

    records, meta = await scraper.scrape()

    if not records:
        logger.warning(f"No records captured for {from_name} ➔ {to_name} ({date_of_journey}).")
        return 0

    # 1. Upsert Operators
    for r in records:
        upsert_operator(
            operator_id=r["operator_id"],
            operator_name=r["operator_name"],
            is_rtc=r.get("is_rtc", 0),
            is_primo=r.get("is_primo", 0),
            max_rating=r.get("rating"),
            rating_count=r.get("review_count", 0),
            updated_at=r["scraped_at"],
            db_path=db_path
        )

    # 2. Insert Append-Only Fact Snapshots
    inserted = insert_fare_snapshots(records, db_path=db_path)
    logger.info(f"✅ Ingested {inserted} snapshots for {from_name} ➔ {to_name} ({date_of_journey}).")
    return inserted


async def execute_scraping_cycle(
    config_path: str = DEFAULT_WATCHLIST_CONFIG,
    db_path: str = DEFAULT_DB_PATH,
    trigger_alerts: bool = True,
    trigger_pricing: bool = True
) -> Dict[str, Any]:
    """
    Executes one complete batch scraping pass across all watchlist routes
    and forward booking windows (D+0, D+1, D+3, D+7).
    """
    config = load_or_create_watchlist(config_path)
    init_db(db_path)

    routes = [r for r in config.get("routes", []) if r.get("enabled", True)]
    windows = config.get("forward_booking_windows", [0, 1, 3, 7])

    base_ist = get_current_ist_time()
    logger.info("=" * 80)
    logger.info(f"🚀 STARTING SCHEDULED SCRAPING CYCLE (IST: {base_ist.strftime('%Y-%m-%d %H:%M:%S')})")
    logger.info(f"Target Routes: {len(routes)} | Forward Windows: {windows}")
    logger.info("=" * 80)

    total_snapshots_ingested = 0
    route_success_count = 0
    route_failure_count = 0

    for route in routes:
        f_id = route["from_id"]
        f_name = route["from_name"]
        t_id = route["to_id"]
        t_name = route["to_name"]

        for offset in windows:
            target_dt = base_ist + timedelta(days=offset)
            doj_str = target_dt.strftime("%d-%b-%Y")

            try:
                cnt = await scrape_route_with_retry(
                    from_id=f_id,
                    from_name=f_name,
                    to_id=t_id,
                    to_name=t_name,
                    date_of_journey=doj_str,
                    db_path=db_path
                )
                total_snapshots_ingested += cnt
                route_success_count += 1
            except Exception as e:
                route_failure_count += 1
                logger.error(f"❌ Failed to scrape route {f_name} ➔ {t_name} on {doj_str} after retries: {e}")

            # Politeness throttle between route requests
            await asyncio.sleep(2.0)

    logger.info("=" * 80)
    logger.info(f"🏁 BATCH SCRAPE SUMMARY: Ingested {total_snapshots_ingested} records | Successful Jobs: {route_success_count} | Failures: {route_failure_count}")
    logger.info("=" * 80)

    # Post-scrape Step 1: Evaluate Competitor Price & Surge Alerts
    alerts_fired = 0
    if trigger_alerts:
        logger.info("Running post-scrape competitor price anomaly and surge evaluation...")
        try:
            alerts_fired = check_and_dispatch_alerts(db_path=db_path)
        except Exception as e:
            logger.error(f"Alert evaluation error: {e}")

    # Post-scrape Step 2: Compute Fresh Dynamic Pricing Recommendations
    if trigger_pricing:
        logger.info("Updating dynamic yield recommendations across watchlist routes...")
        pricing_engine = DynamicPricingEngine(db_path=db_path)
        for route in routes:
            for offset in windows:
                target_dt = base_ist + timedelta(days=offset)
                doj_str = target_dt.strftime("%d-%b-%Y")
                try:
                    pricing_engine.generate_route_recommendations(
                        from_city_id=route["from_id"],
                        to_city_id=route["to_id"],
                        travel_date=doj_str,
                        save_to_db=True
                    )
                except Exception as e:
                    logger.warning(f"Pricing engine calculation skipped for {route['from_name']} ({doj_str}): {e}")

    return {
        "snapshots_ingested": total_snapshots_ingested,
        "success_jobs": route_success_count,
        "failed_jobs": route_failure_count,
        "alerts_triggered": alerts_fired,
        "completed_at": datetime.now(timezone.utc).isoformat()
    }


def calculate_seconds_until_next_ist_run(target_times: List[str]) -> Tuple[int, str]:
    """
    Computes seconds remaining until the closest scheduled IST time
    (e.g., ['08:00', '14:00', '19:00', '22:00']).
    """
    now_ist = get_current_ist_time()
    today_date = now_ist.date()

    candidates: List[datetime] = []
    for t_str in target_times:
        try:
            h, m = [int(p) for p in t_str.strip().split(":")]
            run_dt = datetime(today_date.year, today_date.month, today_date.day, h, m)
            if run_dt > now_ist:
                candidates.append(run_dt)
            else:
                # Add for tomorrow
                candidates.append(run_dt + timedelta(days=1))
        except Exception:
            continue

    if not candidates:
        return 3600 * 6, "06:00:00"  # Fallback: 6 hours

    next_run = min(candidates)
    delta_secs = int((next_run - now_ist).total_seconds())
    return delta_secs, next_run.strftime("%Y-%m-%d %H:%M IST")


async def run_worker_daemon(
    config_path: str = DEFAULT_WATCHLIST_CONFIG,
    db_path: str = DEFAULT_DB_PATH,
    interval_hours: Optional[float] = None
):
    """
    Continuous worker loop running either:
    1. Fixed daily IST times (08:00, 14:00, 19:00, 22:00).
    2. Fixed hourly interval if specified.
    """
    config = load_or_create_watchlist(config_path)
    schedule_times = config.get("schedule_times_ist", ["08:00", "14:00", "19:00", "22:00"])

    logger.info("🛰️ AUTOMATED TIME-SERIES SCRAPER WORKER ACTIVATED")
    logger.info(f"Target IST Schedule: {schedule_times} | Watchlist: {config_path}")

    while True:
        if interval_hours and interval_hours > 0:
            sleep_secs = int(interval_hours * 3600)
            next_str = f"in {interval_hours:.1f} hours"
        else:
            sleep_secs, next_str = calculate_seconds_until_next_ist_run(schedule_times)

        logger.info(f"⏳ Next scheduled scraping cycle: {next_str} (Sleeping {sleep_secs}s)...")

        try:
            await asyncio.sleep(sleep_secs)
            await execute_scraping_cycle(
                config_path=config_path,
                db_path=db_path,
                trigger_alerts=True,
                trigger_pricing=True
            )
        except asyncio.CancelledError:
            logger.info("Worker daemon cancelled. Exiting cleanly.")
            break
        except Exception as e:
            logger.error(f"Unexpected error during worker cycle: {e}. Retrying in 60s...")
            await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Automated Time-Series Scraper & Yield Worker")
    parser.add_argument("--run-now", action="store_true", help="Execute one complete scraping cycle immediately and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as background daemon at IST schedule")
    parser.add_argument("--interval-hours", type=float, default=None, help="Optional recurring interval in hours (e.g. 6.0)")
    parser.add_argument("--config", type=str, default=DEFAULT_WATCHLIST_CONFIG, help="Path to watchlist config JSON")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    if args.run_now:
        logger.info("Executing immediate on-demand scraping pass...")
        asyncio.run(execute_scraping_cycle(
            config_path=args.config,
            db_path=args.db_path,
            trigger_alerts=True,
            trigger_pricing=True
        ))
    elif args.daemon:
        asyncio.run(run_worker_daemon(
            config_path=args.config,
            db_path=args.db_path,
            interval_hours=args.interval_hours
        ))
    else:
        # Default behavior when invoked without flags: display help or status
        print("Scraper Worker Options:")
        print("  --run-now        : Runs 1 complete batch scrape cycle across all watchlist routes now.")
        print("  --daemon         : Starts the scheduled background worker (08:00, 14:00, 19:00, 22:00 IST).")
        print("  --interval-hours : Runs every N hours continuously.")
        print(f"Watchlist configuration file: '{args.config}'")
        load_or_create_watchlist(args.config)


if __name__ == "__main__":
    main()
