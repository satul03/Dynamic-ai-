"""
main.py - CLI Orchestrator & Dynamic Pricing Intelligence Pipeline

Usage Examples:
    python main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026"
    python main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026" --days-ahead 0,1,3
    python main.py --from-id 733 --from-name "Delhi" --to-id 807 --to-name "Jaipur (Rajasthan)" --date "27-Oct-2026" --operator-name "Zingbus"
"""

import argparse
import asyncio
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from tabulate import tabulate

from db import init_db, upsert_city, upsert_operator, insert_fare_snapshots, get_operator_summary, DEFAULT_DB_PATH
from scraper import RedBusScraper

sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger("DynamicPricingCLI")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="redBus Competitor Dynamic Pricing & Relational Storage Pipeline"
    )
    parser.add_argument("--from-id", type=int, default=733, help="Numeric City ID for origin (e.g., 733 for Delhi)")
    parser.add_argument("--from-name", type=str, default="Delhi", help="Origin city name")
    parser.add_argument("--to-id", type=int, default=807, help="Numeric City ID for destination (e.g., 807 for Jaipur)")
    parser.add_argument("--to-name", type=str, default="Jaipur (Rajasthan)", help="Destination city name")
    parser.add_argument("--date", type=str, default="27-Oct-2026", help="Date of journey (format: DD-MMM-YYYY, e.g. 27-Oct-2026)")
    parser.add_argument("--days-ahead", type=str, default=None, help="Comma-separated forward days offsets, e.g. '0,1,3,7'")
    parser.add_argument("--operator-id", type=int, default=None, help="Optional filter by numeric Operator ID")
    parser.add_argument("--operator-name", type=str, default=None, help="Optional filter by Operator Name substring")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database file")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode (default: True)")
    parser.add_argument("--eval-pricing", action="store_true", default=False, help="Automatically evaluate dynamic yield pricing recommendations after scrape")
    parser.add_argument("--check-alerts", action="store_true", default=False, help="Check and dispatch competitor price & surge alerts after scrape")
    return parser.parse_args()


def calculate_journey_dates(base_date_str: str, days_ahead_arg: Optional[str]) -> List[str]:
    """Parses base date and expands forward-looking travel dates if --days-ahead is specified."""
    base_dt = datetime.strptime(base_date_str, "%d-%b-%Y")
    if not days_ahead_arg:
        return [base_date_str]

    dates = []
    offsets = [int(d.strip()) for d in days_ahead_arg.split(",") if d.strip().isdigit()]
    for offset in sorted(offsets):
        target_dt = base_dt + timedelta(days=offset)
        dates.append(target_dt.strftime("%d-%b-%Y"))
    return dates


async def run_pipeline(args):
    db_path = args.db_path
    logger.info(f"Initializing relational database at '{db_path}'...")
    init_db(db_path)

    # 1. Upsert Origin and Destination Cities
    upsert_city(city_id=args.from_id, city_name=args.from_name, db_path=db_path)
    upsert_city(city_id=args.to_id, city_name=args.to_name, db_path=db_path)

    journey_dates = calculate_journey_dates(args.date, args.days_ahead)
    logger.info(f"Scheduled scraping for {len(journey_dates)} journey date(s): {journey_dates}")

    all_scraped_records = []

    for doj in journey_dates:
        print("\n" + "=" * 90)
        print(f"SCRAPING ROUTE: {args.from_name} (ID: {args.from_id}) -> {args.to_name} (ID: {args.to_id}) | DOJ: {doj}")
        print("=" * 90)

        scraper = RedBusScraper(
            from_city_id=args.from_id,
            from_city_name=args.from_name,
            to_city_id=args.to_id,
            to_city_name=args.to_name,
            date_of_journey=doj,
            operator_filter_id=args.operator_id,
            operator_filter_name=args.operator_name,
            headless=args.headless
        )

        records, meta = await scraper.scrape()
        all_scraped_records.extend(records)

        # 2. Upsert Discovered Operators
        logger.info(f"Upserting {len(set(r['operator_id'] for r in records))} operators into database...")
        for r in records:
            upsert_operator(
                operator_id=r["operator_id"],
                operator_name=r["operator_name"],
                is_rtc=r["is_rtc"],
                is_primo=r["is_primo"],
                max_rating=r["rating"],
                rating_count=r.get("review_count", 0),
                updated_at=r["scraped_at"],
                db_path=db_path
            )

        # 3. Insert Append-Only Fare Snapshots
        inserted_count = insert_fare_snapshots(records, db_path=db_path)
        logger.info(f"Successfully inserted {inserted_count} dynamic fare snapshots for DOJ {doj}.")

    # 4. Generate & Print Analytical Summary Table
    print("\n" + "#" * 90)
    print(f"MARKET INTELLIGENCE SUMMARY: {args.from_name} -> {args.to_name} (DOJ: {args.date})")
    print("#" * 90)

    summary_df = get_operator_summary(
        from_city_id=args.from_id,
        to_city_id=args.to_id,
        travel_date=args.date if len(journey_dates) == 1 else None,
        db_path=db_path
    )

    if not summary_df.empty:
        # Format columns for presentation
        display_df = summary_df.copy()
        display_df["Category"] = display_df["is_rtc"].apply(lambda x: "RTC Gov" if x == 1 else "Private")
        display_df["Primo"] = display_df["is_primo"].apply(lambda x: "Yes" if x == 1 else "No")
        display_df["Min Fare (₹)"] = display_df["min_fare"].apply(lambda x: f"₹{int(x)}" if pd.notnull(x) else "-")
        display_df["Median Fare (₹)"] = display_df["median_fare"].apply(lambda x: f"₹{int(x)}" if pd.notnull(x) else "-")
        display_df["Max Fare (₹)"] = display_df["max_fare"].apply(lambda x: f"₹{int(x)}" if pd.notnull(x) else "-")
        display_df["Avg Disc %"] = display_df["avg_discount_pct"].apply(lambda x: f"{x}%" if pd.notnull(x) else "0.0%")
        display_df["Seats Avail"] = display_df["total_seats_available"].apply(lambda x: int(x) if pd.notnull(x) else 0)
        display_df["Rating"] = display_df["avg_rating"].apply(lambda x: f"{x:.1f} ★" if pd.notnull(x) and x > 0 else "N/A")
        display_df["Rating Count"] = display_df["total_rating_count"].apply(lambda x: f"{int(x):,}" if pd.notnull(x) and x > 0 else "-")

        table_cols = [
            "operator_id", "operator_name", "Category", "Primo",
            "total_buses", "Min Fare (₹)", "Median Fare (₹)", "Max Fare (₹)",
            "Avg Disc %", "Seats Avail", "Rating", "Rating Count"
        ]
        display_table = display_df[table_cols].rename(columns={
            "operator_id": "Op ID",
            "operator_name": "Operator Name",
            "total_buses": "Buses"
        })

        print(tabulate(display_table, headers="keys", tablefmt="grid", showindex=False))

        # Executive Metrics
        total_unique_ops = summary_df["operator_id"].nunique()
        total_services = summary_df["total_buses"].sum()
        rtc_buses = summary_df[summary_df["is_rtc"] == 1]["total_buses"].sum()
        overall_min = summary_df["min_fare"].min()
        overall_max = summary_df["max_fare"].max()
        total_rating_count = summary_df["total_rating_count"].sum()

        print("\n" + "-" * 90)
        print("EXECUTIVE MARKET OVERVIEW:")
        print(f"  • Route:                      {args.from_name} (ID: {args.from_id}) -> {args.to_name} (ID: {args.to_id})")
        print(f"  • Total Operators Discovered: {total_unique_ops}")
        print(f"  • Total Bus Services Scraped: {total_services}")
        print(f"  • Total Ratings / Reviews:    {int(total_rating_count):,}")
        print(f"  • Government RTC Services:    {rtc_buses} ({round(rtc_buses/total_services*100, 1) if total_services else 0}%)")
        print(f"  • Market Dynamic Fare Range:  ₹{int(overall_min)} - ₹{int(overall_max)}")
        print(f"  • Database Persistence:       {os.path.abspath(db_path)} (Size: {os.path.getsize(db_path):,} bytes)")
        print("-" * 90 + "\n")
    else:
        print("No bus records found matching criteria.")

    # 5. Optional Algorithmic Dynamic Pricing Evaluation
    if args.eval_pricing:
        from pricing_engine import DynamicPricingEngine
        print("\n" + "=" * 90)
        print("GENERATING DYNAMIC PRICING YIELD RECOMMENDATIONS...")
        print("=" * 90)
        engine = DynamicPricingEngine(db_path=db_path)
        engine.generate_route_recommendations(
            from_city_id=args.from_id,
            to_city_id=args.to_id,
            travel_date=args.date,
            save_to_db=True
        )

    # 6. Optional Competitor Price & Surge Alerts Evaluation
    if args.check_alerts:
        from alerts import check_and_dispatch_alerts
        check_and_dispatch_alerts(
            db_path=db_path,
            from_city_id=args.from_id,
            to_city_id=args.to_id,
            travel_date=args.date
        )


def main():
    import pandas as pd
    globals()['pd'] = pd
    args = parse_arguments()
    asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    main()
