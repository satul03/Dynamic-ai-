"""
pricing_engine.py - Algorithmic Dynamic Pricing & Yield Management Engine

Role & Purpose:
Connects to SQLite (bus_pricing_intelligence.db) and generates revenue-maximizing dynamic
pricing recommendations for bus operators based on:
1. Micro-market Competitor Benchmarking (median and 75th percentile of matching departure window & coach tier).
2. Seat Inventory & Load Factor Velocity (distress discounting vs. scarcity surge).
3. Berth / Seat Tier Segmentation (Standard/Upper @ 1.0x, Lower Deck @ 1.12x, Single Sleeper @ 1.22x).

Usage Examples:
    python pricing_engine.py --from-id 733 --to-id 807 --date "27-Oct-2026"
    python pricing_engine.py --from-id 733 --to-id 807 --date "27-Oct-2026" --operator "NueGo" --save-db
"""

import sys
import os
import argparse
import logging
import math
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
from tabulate import tabulate

from db import (
    get_db_connection,
    insert_pricing_recommendations,
    DEFAULT_DB_PATH
)

# Configure stdout and logging
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logger = logging.getLogger("PricingEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class YieldRecommendation:
    recommendation_id: str
    created_at: str
    from_city_id: int
    to_city_id: int
    travel_date: str
    service_id: str
    operator_name: str
    bus_type: str
    departure_time: str
    hours_to_departure: float
    total_seats: int
    seats_available: int
    occupancy_pct: float
    competitor_min_fare: float
    competitor_median_fare: float
    competitor_p75_fare: float
    competitor_count: int
    pricing_strategy: str
    occupancy_multiplier: float
    base_recommended_fare: float
    standard_upper_fare: float
    lower_deck_fare: float
    single_sleeper_fare: float
    notes: str


class DynamicPricingEngine:
    """
    Computes algorithmic ticket price recommendations based on real-time redBus
    competitor benchmarks, occupancy load factors, and seat berth tier segmentation.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def _parse_time_to_minutes(self, time_str: str) -> int:
        """Converts 'HH:MM' or 'HH:MM:SS' string into total minutes from midnight."""
        if not time_str or ":" not in str(time_str):
            return 720  # Midday default
        parts = str(time_str).strip().split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 720

    def compute_occupancy_strategy(
        self,
        occupancy_pct: float,
        hours_to_departure: float
    ) -> Tuple[str, float, str]:
        """
        Determines the dynamic yield multiplier based on load factor & time-to-departure.
        Rules:
          - Scarcity Surge: Occupancy > 85% (+35% scarcity premium)
          - High Demand Surge: 65% < Occupancy <= 85% (+15% surge)
          - Distress Discount: Occupancy < 30% and <= 6h to dep (-15% discount)
          - Early-Bird Incentive: Occupancy < 40% and > 24h to dep (-8% incentive)
          - Off-Peak Stimulation: Occupancy < 40% and 6h < dep <= 24h (-5% value discount)
          - Market Parity: 40% <= Occupancy <= 65% (1.0x market median)
        """
        if occupancy_pct > 85.0:
            return (
                "SCARCITY_SURGE",
                1.35,
                f"Critical Scarcity Surge: Low inventory remaining ({100 - occupancy_pct:.1f}% unbooked). Extracting +35% scarcity premium."
            )
        elif 65.0 < occupancy_pct <= 85.0:
            return (
                "HIGH_DEMAND_SURGE",
                1.15,
                f"High Demand Momentum: Strong load factor ({occupancy_pct:.1f}%). Capturing +15% yield surge."
            )
        elif occupancy_pct < 30.0 and hours_to_departure <= 6.0:
            return (
                "DISTRESS_DISCOUNT",
                0.85,
                f"Distress Liquidation: {occupancy_pct:.1f}% load inside {hours_to_departure:.1f}h. Applying -15% markdown."
            )
        elif occupancy_pct < 40.0 and hours_to_departure > 24.0:
            return (
                "EARLY_BIRD_INCENTIVE",
                0.92,
                f"Early Bird Stimulation: {occupancy_pct:.1f}% load >24h ahead. Applying -8% discount to accelerate velocity."
            )
        elif occupancy_pct < 40.0:
            return (
                "VALUE_OFF_PEAK",
                0.95,
                f"Off-Peak Value Stimulation: Sub-optimal load factor ({occupancy_pct:.1f}%) inside 24h. Applying -5% markdown."
            )
        else:  # 40.0 <= occupancy_pct <= 65.0
            return (
                "MARKET_PARITY",
                1.00,
                f"Market Follower: Balanced load factor ({occupancy_pct:.1f}%). Pegging directly to competitor median."
            )

    def calculate_berth_tiers(self, base_fare: float) -> Tuple[float, float, float]:
        """
        Calculates berth-level pricing segmentation:
        1. Standard / Upper Berth: 1.00x base
        2. Lower Deck Berth: 1.12x (+12% accessibility & ride comfort premium)
        3. Single Sleeper Berth: 1.22x (+22% solitude & privacy premium)
        All fares are rounded to the nearest ₹10.
        """
        standard = round(base_fare / 10.0) * 10.0
        lower = round((base_fare * 1.12) / 10.0) * 10.0
        single = round((base_fare * 1.22) / 10.0) * 10.0
        return standard, lower, single

    def recommend_for_service(
        self,
        from_city_id: int,
        to_city_id: int,
        travel_date: str,
        service_id: str,
        operator_name: str,
        bus_type: str,
        departure_time: str,
        hours_to_departure: float,
        seats_available: int,
        total_seats: int,
        is_ac: int = 1,
        is_sleeper: int = 1,
        time_window_minutes: int = 90
    ) -> YieldRecommendation:
        """
        Computes dynamic yield recommendation for a single bus service by comparing
        it against all competitor services operating within +/- time_window_minutes
        with matching coach tiers.
        """
        dep_mins = self._parse_time_to_minutes(departure_time)
        win_start = max(0, dep_mins - time_window_minutes)
        win_end = min(1440, dep_mins + time_window_minutes)

        # 1. Fetch Competitor Snapshot Data
        with get_db_connection(self.db_path) as conn:
            query = """
                SELECT 
                    operator_name,
                    effective_fare,
                    seats_available,
                    departure_time,
                    is_ac,
                    is_sleeper
                FROM fare_snapshots
                WHERE from_city_id = ? 
                  AND to_city_id = ? 
                  AND travel_date = ?
                  AND service_id != ?
                  AND is_ac = ?
                  AND is_sleeper = ?
                  AND effective_fare > 0;
            """
            comp_df = pd.read_sql_query(
                query,
                conn,
                params=[from_city_id, to_city_id, travel_date, service_id, is_ac, is_sleeper]
            )

        # Filter to proximate departure window
        if not comp_df.empty:
            comp_df["dep_mins"] = comp_df["departure_time"].apply(self._parse_time_to_minutes)
            comp_window = comp_df[(comp_df["dep_mins"] >= win_start) & (comp_df["dep_mins"] <= win_end)]
            if comp_window.empty:
                comp_window = comp_df  # Fallback to entire route day if window is sparse
        else:
            comp_window = comp_df

        # 2. Competitor Benchmarking Metrics
        if not comp_window.empty:
            c_min = float(comp_window["effective_fare"].min())
            c_med = float(comp_window["effective_fare"].median())
            c_p75 = float(comp_window["effective_fare"].quantile(0.75))
            c_count = len(comp_window)
        else:
            # Fallback heuristic if no competitors are found on this route
            c_min = 400.0
            c_med = 550.0
            c_p75 = 650.0
            c_count = 0

        # 3. Compute Load Factor
        tot_seats = max(1, total_seats if total_seats and total_seats > 0 else 40)
        seats_left = max(0, min(tot_seats, seats_available))
        booked_seats = tot_seats - seats_left
        occupancy_pct = (booked_seats / tot_seats) * 100.0

        # 4. Strategy & Multiplier
        strategy, multiplier, notes = self.compute_occupancy_strategy(
            occupancy_pct=occupancy_pct,
            hours_to_departure=hours_to_departure
        )

        # 5. Base Recommended Fare
        raw_base = c_med * multiplier
        # Ensure rate does not breach extreme market limits
        bounded_base = max(c_min * 0.90, min(c_p75 * 1.30, raw_base))
        base_recommended = round(bounded_base / 10.0) * 10.0

        # 6. Berth Tier Segmentation
        standard_fare, lower_fare, single_fare = self.calculate_berth_tiers(base_recommended)

        rec_seed = f"{service_id}_{travel_date}_{int(base_recommended)}_{int(occupancy_pct)}"
        rec_id = f"REC_{zlib.crc32(rec_seed.encode('utf-8')):08x}"

        return YieldRecommendation(
            recommendation_id=rec_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            from_city_id=from_city_id,
            to_city_id=to_city_id,
            travel_date=travel_date,
            service_id=service_id,
            operator_name=operator_name,
            bus_type=bus_type,
            departure_time=departure_time,
            hours_to_departure=hours_to_departure,
            total_seats=tot_seats,
            seats_available=seats_left,
            occupancy_pct=round(occupancy_pct, 1),
            competitor_min_fare=c_min,
            competitor_median_fare=c_med,
            competitor_p75_fare=c_p75,
            competitor_count=c_count,
            pricing_strategy=strategy,
            occupancy_multiplier=multiplier,
            base_recommended_fare=base_recommended,
            standard_upper_fare=standard_fare,
            lower_deck_fare=lower_fare,
            single_sleeper_fare=single_fare,
            notes=notes
        )

    def generate_route_recommendations(
        self,
        from_city_id: int,
        to_city_id: int,
        travel_date: str,
        target_operator_name: Optional[str] = None,
        save_to_db: bool = False
    ) -> pd.DataFrame:
        """
        Runs the dynamic pricing engine across all unique services on a target route.
        """
        with get_db_connection(self.db_path) as conn:
            op_filter = "AND operator_name LIKE ?" if target_operator_name else ""
            params: List[Any] = [from_city_id, to_city_id, travel_date]
            if target_operator_name:
                params.append(f"%{target_operator_name}%")

            query = f"""
                SELECT 
                    service_id,
                    operator_name,
                    bus_type,
                    departure_time,
                    hours_to_departure,
                    seats_available,
                    total_seats,
                    is_ac,
                    is_sleeper,
                    effective_fare,
                    MAX(scraped_at) as latest_scrape
                FROM fare_snapshots
                WHERE from_city_id = ? AND to_city_id = ? AND travel_date = ? {op_filter}
                GROUP BY service_id
                ORDER BY departure_time ASC;
            """
            services_df = pd.read_sql_query(query, conn, params=params)

        if services_df.empty:
            logger.warning(f"No services found for route {from_city_id} ➔ {to_city_id} on {travel_date}.")
            return pd.DataFrame()

        logger.info(f"Computing yield recommendations for {len(services_df)} services on route {from_city_id} ➔ {to_city_id}...")

        recommendations: List[YieldRecommendation] = []
        for _, row in services_df.iterrows():
            rec = self.recommend_for_service(
                from_city_id=from_city_id,
                to_city_id=to_city_id,
                travel_date=travel_date,
                service_id=str(row["service_id"]),
                operator_name=str(row["operator_name"]),
                bus_type=str(row["bus_type"]),
                departure_time=str(row["departure_time"]),
                hours_to_departure=float(row["hours_to_departure"]) if pd.notnull(row["hours_to_departure"]) else 12.0,
                seats_available=int(row["seats_available"]) if pd.notnull(row["seats_available"]) else 20,
                total_seats=int(row["total_seats"]) if pd.notnull(row["total_seats"]) else 40,
                is_ac=int(row["is_ac"]) if pd.notnull(row["is_ac"]) else 1,
                is_sleeper=int(row["is_sleeper"]) if pd.notnull(row["is_sleeper"]) else 1
            )
            recommendations.append(rec)

        if save_to_db and recommendations:
            raw_dicts = [asdict(r) for r in recommendations]
            saved_cnt = insert_pricing_recommendations(raw_dicts, db_path=self.db_path)
            logger.info(f"Persisted {saved_cnt} pricing recommendations into table 'pricing_recommendations'.")

        rec_df = pd.DataFrame([asdict(r) for r in recommendations])
        return rec_df


def main():
    parser = argparse.ArgumentParser(description="Algorithmic Dynamic Pricing & Yield Recommendation Engine")
    parser.add_argument("--from-id", type=int, default=733, help="Origin City ID (default: 733 Delhi)")
    parser.add_argument("--to-id", type=int, default=807, help="Destination City ID (default: 807 Jaipur)")
    parser.add_argument("--date", type=str, default="27-Oct-2026", help="Date of journey (DD-MMM-YYYY)")
    parser.add_argument("--operator", type=str, default=None, help="Optional operator filter (e.g. 'NueGo')")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--save-db", action="store_true", help="Persist calculated recommendations into SQLite")
    args = parser.parse_args()

    engine = DynamicPricingEngine(db_path=args.db_path)
    df = engine.generate_route_recommendations(
        from_city_id=args.from_id,
        to_city_id=args.to_id,
        travel_date=args.date,
        target_operator_name=args.operator,
        save_to_db=args.save_db
    )

    if df.empty:
        print("No dynamic pricing recommendations could be generated.")
        return

    print("\n" + "=" * 115)
    print(f"🎯 DYNAMIC YIELD PRICING RECOMMENDATIONS (Route: {args.from_id} ➔ {args.to_id} | DOJ: {args.date})")
    print("=" * 115)

    display_cols = [
        "departure_time", "operator_name", "occupancy_pct", "pricing_strategy",
        "competitor_median_fare", "occupancy_multiplier",
        "standard_upper_fare", "lower_deck_fare", "single_sleeper_fare"
    ]

    table_df = df[display_cols].copy().head(20)
    table_df["occupancy_pct"] = table_df["occupancy_pct"].apply(lambda x: f"{x:.1f}%")
    table_df["competitor_median_fare"] = table_df["competitor_median_fare"].apply(lambda x: f"₹{int(x):,}")
    table_df["occupancy_multiplier"] = table_df["occupancy_multiplier"].apply(lambda x: f"{x:.2f}x")
    table_df["standard_upper_fare"] = table_df["standard_upper_fare"].apply(lambda x: f"₹{int(x):,}")
    table_df["lower_deck_fare"] = table_df["lower_deck_fare"].apply(lambda x: f"₹{int(x):,}")
    table_df["single_sleeper_fare"] = table_df["single_sleeper_fare"].apply(lambda x: f"₹{int(x):,}")

    formatted = table_df.rename(columns={
        "departure_time": "Dep Time",
        "operator_name": "Operator",
        "occupancy_pct": "Load Factor",
        "pricing_strategy": "Strategy",
        "competitor_median_fare": "Market Med (₹)",
        "occupancy_multiplier": "Load Mult",
        "standard_upper_fare": "Upper (1.0x)",
        "lower_deck_fare": "Lower (1.12x)",
        "single_sleeper_fare": "Single (1.22x)",
    })

    print(tabulate(formatted, headers="keys", tablefmt="grid", showindex=False))
    print("=" * 115)

    # Executive Summary Metrics
    print("\nYIELD MANAGEMENT EXECUTIVE BENCHMARK:")
    print(f"  • Total Services Evaluated:         {len(df)}")
    print(f"  • Average Market Median Dynamic Fare: ₹{int(df['competitor_median_fare'].mean()):,}")
    print(f"  • Recommended Standard Base Range:   ₹{int(df['standard_upper_fare'].min()):,} - ₹{int(df['standard_upper_fare'].max()):,}")
    print(f"  • Recommended Single Sleeper Range:  ₹{int(df['single_sleeper_fare'].min()):,} - ₹{int(df['single_sleeper_fare'].max()):,}")
    print(f"  • Strategy Distribution:             {dict(df['pricing_strategy'].value_counts())}")
    print(f"  • Database Persistence Status:       {'Persisted to SQLite pricing_recommendations' if args.save_db else 'In-memory (use --save-db to persist)'}\n")


if __name__ == "__main__":
    main()
