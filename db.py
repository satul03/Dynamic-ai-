"""
db.py - Relational SQLite Database Layer for Competitor Bus Dynamic Pricing Pipeline

Schema:
1. cities: Dimension table for origin and destination cities
2. operators: Dimension table for intercity bus operators (RTC government & private)
3. fare_snapshots: Fact table for time-series dynamic pricing, load factors, & deals
"""

import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import pandas as pd

logger = logging.getLogger("BusPricingDB")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "bus_pricing_intelligence.db")


def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Creates a connection with SQLite foreign keys and WAL mode enabled for performance."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Initializes tables and indexes for the normalized dynamic pricing schema."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Cities Dimension Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                city_id INTEGER PRIMARY KEY,
                city_name TEXT NOT NULL,
                state TEXT,
                country TEXT DEFAULT 'India'
            );
        """)

        # 2. Operators Dimension Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                operator_id INTEGER PRIMARY KEY,
                operator_name TEXT NOT NULL,
                is_rtc INTEGER DEFAULT 0,
                is_primo INTEGER DEFAULT 0,
                max_rating REAL,
                rating_count INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );
        """)

        # 3. Fare Snapshots Fact Table (Append-only time-series)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fare_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                scraped_at TEXT NOT NULL,
                hours_to_departure REAL,
                from_city_id INTEGER NOT NULL,
                to_city_id INTEGER NOT NULL,
                travel_date TEXT NOT NULL,
                operator_id INTEGER NOT NULL,
                operator_name TEXT NOT NULL,
                service_id TEXT NOT NULL,
                bus_type TEXT,
                is_ac INTEGER DEFAULT 0,
                is_sleeper INTEGER DEFAULT 0,
                departure_time TEXT,
                arrival_time TEXT,
                duration TEXT,
                original_fare REAL,
                effective_fare REAL,
                fare_tiers_json TEXT,
                discount_pct REAL,
                total_seats INTEGER,
                seats_available INTEGER,
                single_seats_available INTEGER,
                rating REAL,
                review_count INTEGER,
                boarding_points_json TEXT,
                dropping_points_json TEXT,
                deal_tag TEXT,
                FOREIGN KEY (from_city_id) REFERENCES cities (city_id),
                FOREIGN KEY (to_city_id) REFERENCES cities (city_id),
                FOREIGN KEY (operator_id) REFERENCES operators (operator_id)
            );
        """)

        # 4. Dynamic Pricing & Yield Recommendations Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pricing_recommendations (
                recommendation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                from_city_id INTEGER NOT NULL,
                to_city_id INTEGER NOT NULL,
                travel_date TEXT NOT NULL,
                service_id TEXT NOT NULL,
                operator_name TEXT,
                bus_type TEXT,
                departure_time TEXT,
                hours_to_departure REAL,
                total_seats INTEGER,
                seats_available INTEGER,
                occupancy_pct REAL,
                competitor_min_fare REAL,
                competitor_median_fare REAL,
                competitor_p75_fare REAL,
                competitor_count INTEGER,
                pricing_strategy TEXT,
                occupancy_multiplier REAL,
                base_recommended_fare REAL,
                standard_upper_fare REAL,
                lower_deck_fare REAL,
                single_sleeper_fare REAL,
                notes TEXT,
                FOREIGN KEY (from_city_id) REFERENCES cities (city_id),
                FOREIGN KEY (to_city_id) REFERENCES cities (city_id)
            );
        """)

        # 5. Competitor Price & Surge Alerts Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                alert_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT DEFAULT 'INFO',
                operator_id INTEGER NOT NULL,
                operator_name TEXT NOT NULL,
                service_id TEXT NOT NULL,
                from_city_id INTEGER NOT NULL,
                to_city_id INTEGER NOT NULL,
                travel_date TEXT NOT NULL,
                departure_time TEXT,
                hours_to_departure REAL,
                old_fare REAL,
                new_fare REAL,
                fare_delta REAL,
                fare_delta_pct REAL,
                seats_remaining INTEGER,
                message TEXT,
                dispatched_channels TEXT,
                FOREIGN KEY (from_city_id) REFERENCES cities (city_id),
                FOREIGN KEY (to_city_id) REFERENCES cities (city_id)
            );
        """)

        # Fast analytical query indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fare_snapshots_route_date ON fare_snapshots (from_city_id, to_city_id, travel_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fare_snapshots_operator ON fare_snapshots (operator_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fare_snapshots_service_scraped ON fare_snapshots (service_id, scraped_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pricing_rec_route ON pricing_recommendations (from_city_id, to_city_id, travel_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_created ON price_alerts (created_at DESC);")

        # Ensure rating_count column exists in operators table for backward compatibility
        cursor.execute("PRAGMA table_info(operators);")
        op_cols = [row["name"] for row in cursor.fetchall()]
        if "rating_count" not in op_cols:
            cursor.execute("ALTER TABLE operators ADD COLUMN rating_count INTEGER DEFAULT 0;")

        conn.commit()
        logger.info(f"Database schema initialized successfully at '{db_path}'.")


def upsert_city(
    city_id: int,
    city_name: str,
    state: Optional[str] = None,
    country: str = "India",
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """Upserts a city record using INSERT OR REPLACE / ON CONFLICT."""
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO cities (city_id, city_name, state, country)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(city_id) DO UPDATE SET
                city_name = excluded.city_name,
                state = COALESCE(excluded.state, cities.state),
                country = COALESCE(excluded.country, cities.country);
        """, (city_id, city_name, state, country))
        conn.commit()


def upsert_operator(
    operator_id: int,
    operator_name: str,
    is_rtc: int = 0,
    is_primo: int = 0,
    max_rating: Optional[float] = None,
    rating_count: Optional[int] = 0,
    updated_at: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """Upserts an operator record maintaining highest rating, rating count, and flags."""
    now_iso = updated_at or datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO operators (operator_id, operator_name, is_rtc, is_primo, max_rating, rating_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id) DO UPDATE SET
                operator_name = excluded.operator_name,
                is_rtc = MAX(operators.is_rtc, excluded.is_rtc),
                is_primo = MAX(operators.is_primo, excluded.is_primo),
                max_rating = MAX(COALESCE(operators.max_rating, 0), COALESCE(excluded.max_rating, 0)),
                rating_count = MAX(COALESCE(operators.rating_count, 0), COALESCE(excluded.rating_count, 0)),
                updated_at = excluded.updated_at;
        """, (operator_id, operator_name, is_rtc, is_primo, max_rating, rating_count or 0, now_iso))
        conn.commit()


def insert_fare_snapshots(
    snapshots: List[Dict[str, Any]],
    db_path: str = DEFAULT_DB_PATH
) -> int:
    """Batch inserts append-only fare snapshots for time-series dynamic pricing."""
    if not snapshots:
        return 0

    sql = """
        INSERT INTO fare_snapshots (
            snapshot_id, scraped_at, hours_to_departure, from_city_id, to_city_id,
            travel_date, operator_id, operator_name, service_id, bus_type,
            is_ac, is_sleeper, departure_time, arrival_time, duration,
            original_fare, effective_fare, fare_tiers_json, discount_pct,
            total_seats, seats_available, single_seats_available, rating,
            review_count, boarding_points_json, dropping_points_json, deal_tag
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(snapshot_id) DO NOTHING;
    """

    records = []
    for s in snapshots:
        records.append((
            s.get("snapshot_id"),
            s.get("scraped_at"),
            s.get("hours_to_departure"),
            s.get("from_city_id"),
            s.get("to_city_id"),
            s.get("travel_date"),
            s.get("operator_id"),
            s.get("operator_name"),
            s.get("service_id"),
            s.get("bus_type"),
            s.get("is_ac", 0),
            s.get("is_sleeper", 0),
            s.get("departure_time"),
            s.get("arrival_time"),
            s.get("duration"),
            s.get("original_fare"),
            s.get("effective_fare"),
            s.get("fare_tiers_json") if isinstance(s.get("fare_tiers_json"), str) else json.dumps(s.get("fare_tiers_json") or []),
            s.get("discount_pct", 0.0),
            s.get("total_seats"),
            s.get("seats_available"),
            s.get("single_seats_available"),
            s.get("rating"),
            s.get("review_count", 0),
            s.get("boarding_points_json") if isinstance(s.get("boarding_points_json"), str) else json.dumps(s.get("boarding_points_json") or []),
            s.get("dropping_points_json") if isinstance(s.get("dropping_points_json"), str) else json.dumps(s.get("dropping_points_json") or []),
            s.get("deal_tag")
        ))

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, records)
        inserted_count = cursor.rowcount
        conn.commit()
        return inserted_count


def get_operator_summary(
    from_city_id: int,
    to_city_id: int,
    travel_date: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> pd.DataFrame:
    """
    Returns operator dynamic pricing metrics:
    Operator ID, Operator Name, Total Services, Min Fare, Median Fare, Max Fare, Available Seats, Avg Rating.
    """
    query = """
        SELECT
            s.operator_id,
            s.operator_name,
            o.is_rtc,
            o.is_primo,
            COUNT(s.service_id) as total_buses,
            MIN(s.effective_fare) as min_fare,
            AVG(s.effective_fare) as avg_fare,
            MAX(s.effective_fare) as max_fare,
            ROUND(AVG(COALESCE(s.discount_pct, 0)), 1) as avg_discount_pct,
            SUM(COALESCE(s.seats_available, 0)) as total_seats_available,
            ROUND(AVG(s.rating), 2) as avg_rating,
            SUM(COALESCE(s.review_count, 0)) as total_rating_count
        FROM fare_snapshots s
        LEFT JOIN operators o ON s.operator_id = o.operator_id
        WHERE s.from_city_id = ? AND s.to_city_id = ?
    """
    params: List[Any] = [from_city_id, to_city_id]
    if travel_date:
        query += " AND s.travel_date = ?"
        params.append(travel_date)

    query += " GROUP BY s.operator_id, s.operator_name ORDER BY total_buses DESC, min_fare ASC;"

    with get_db_connection(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    # Compute median fare per operator using raw snapshot values if rows exist
    if not df.empty:
        with get_db_connection(db_path) as conn:
            raw_query = """
                SELECT operator_id, effective_fare
                FROM fare_snapshots
                WHERE from_city_id = ? AND to_city_id = ?
            """
            raw_params: List[Any] = [from_city_id, to_city_id]
            if travel_date:
                raw_query += " AND travel_date = ?"
                raw_params.append(travel_date)
            fares_df = pd.read_sql_query(raw_query, conn, params=raw_params)
            medians = fares_df.groupby("operator_id")["effective_fare"].median().to_dict()
            df["median_fare"] = df["operator_id"].map(medians)

    return df


def insert_pricing_recommendations(
    recommendations: List[Dict[str, Any]],
    db_path: str = DEFAULT_DB_PATH
) -> int:
    """Inserts generated yield management pricing recommendations into the database."""
    if not recommendations:
        return 0

    sql = """
        INSERT OR REPLACE INTO pricing_recommendations (
            recommendation_id, created_at, from_city_id, to_city_id, travel_date,
            service_id, operator_name, bus_type, departure_time, hours_to_departure,
            total_seats, seats_available, occupancy_pct, competitor_min_fare,
            competitor_median_fare, competitor_p75_fare, competitor_count,
            pricing_strategy, occupancy_multiplier, base_recommended_fare,
            standard_upper_fare, lower_deck_fare, single_sleeper_fare, notes
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?
        );
    """

    rows = []
    for r in recommendations:
        rows.append((
            r.get("recommendation_id"),
            r.get("created_at") or datetime.now(timezone.utc).isoformat(),
            r.get("from_city_id"),
            r.get("to_city_id"),
            r.get("travel_date"),
            r.get("service_id"),
            r.get("operator_name"),
            r.get("bus_type"),
            r.get("departure_time"),
            r.get("hours_to_departure"),
            r.get("total_seats"),
            r.get("seats_available"),
            r.get("occupancy_pct"),
            r.get("competitor_min_fare"),
            r.get("competitor_median_fare"),
            r.get("competitor_p75_fare"),
            r.get("competitor_count"),
            r.get("pricing_strategy"),
            r.get("occupancy_multiplier"),
            r.get("base_recommended_fare"),
            r.get("standard_upper_fare"),
            r.get("lower_deck_fare"),
            r.get("single_sleeper_fare"),
            r.get("notes")
        ))

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, rows)
        inserted = cursor.rowcount
        conn.commit()
        return inserted


def insert_price_alerts(
    alerts: List[Dict[str, Any]],
    db_path: str = DEFAULT_DB_PATH
) -> int:
    """Stores triggered competitor price drop or surge alerts."""
    if not alerts:
        return 0

    sql = """
        INSERT OR REPLACE INTO price_alerts (
            alert_id, created_at, alert_type, severity, operator_id, operator_name,
            service_id, from_city_id, to_city_id, travel_date, departure_time,
            hours_to_departure, old_fare, new_fare, fare_delta, fare_delta_pct,
            seats_remaining, message, dispatched_channels
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?
        );
    """

    rows = []
    for a in alerts:
        rows.append((
            a.get("alert_id"),
            a.get("created_at") or datetime.now(timezone.utc).isoformat(),
            a.get("alert_type"),
            a.get("severity", "INFO"),
            a.get("operator_id"),
            a.get("operator_name"),
            a.get("service_id"),
            a.get("from_city_id"),
            a.get("to_city_id"),
            a.get("travel_date"),
            a.get("departure_time"),
            a.get("hours_to_departure"),
            a.get("old_fare"),
            a.get("new_fare"),
            a.get("fare_delta"),
            a.get("fare_delta_pct"),
            a.get("seats_remaining"),
            a.get("message"),
            a.get("dispatched_channels", "LOG")
        ))

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, rows)
        inserted = cursor.rowcount
        conn.commit()
        return inserted


def get_latest_alerts(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Fetches recently triggered alerts for reporting and UI dashboard display."""
    with get_db_connection(db_path) as conn:
        query = "SELECT * FROM price_alerts ORDER BY created_at DESC LIMIT ?;"
        return pd.read_sql_query(query, conn, params=[limit])


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
