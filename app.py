"""
app.py - Competitor Dynamic Pricing & Market Intelligence Platform
A production-ready Streamlit dashboard for intercity bus operators on redBus.

Technical Stack:
- Framework: Streamlit
- Database: SQLite (bus_pricing_intelligence.db via db.py)
- Scraping Engine: Playwright via main.py subprocess orchestration
- Visualizations: Plotly Express & Graph Objects
- Reporting: Openpyxl (Multi-tab formatted Excel export) & Pandas
"""

import os
import sys
import json
import sqlite3
import io
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Import database layer
from db import (
    get_db_connection,
    init_db,
    upsert_city,
    upsert_operator,
    DEFAULT_DB_PATH,
)

# Import security & authentication layer
from auth import (
    is_authenticated,
    render_login_screen,
    render_sidebar_auth_widget,
    get_current_user,
    logout_user
)

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Bus Pricing Intelligence | Competitor Dashboard",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom High-Polish CSS styling
st.markdown("""
<style>
    /* Metric Card Styling */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.02));
        border: 1px solid rgba(128, 128, 128, 0.2);
        padding: 16px 20px;
        border-radius: 12px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    div[data-testid="stMetric"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(0, 0, 0, 0.1);
        border-color: #D84E55;
    }
    div[data-testid="stMetricLabel"] p {
        font-size: 0.88rem !important;
        font-weight: 600 !important;
        color: #888888 !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    div[data-testid="stMetricValue"] div {
        font-size: 1.8rem !important;
        font-weight: 700 !important;
        color: #D84E55 !important;
    }

    /* Badges & Pills */
    .badge-rtc {
        background-color: #2563EB;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-private {
        background-color: #4B5563;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-primo {
        background-color: #10B981;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
    }

    /* Header Accent */
    .brand-title {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(90deg, #D84E55, #E11D48, #F97316);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0px;
    }
    .brand-subtitle {
        color: #6B7280;
        font-size: 1.0rem;
        margin-top: -6px;
        margin-bottom: 18px;
    }

    /* Live fetch card in sidebar */
    .scrape-card {
        background: rgba(216, 78, 85, 0.08);
        border: 1px solid rgba(216, 78, 85, 0.3);
        border-radius: 10px;
        padding: 12px 14px;
        margin-bottom: 16px;
    }

    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 48px;
        white-space: pre-wrap;
        border-radius: 8px 8px 0px 0px;
        padding: 10px 18px;
        font-weight: 600;
    }

    /* Equal Height Card Grid & Alignment */
    div[data-testid="stColumn"] > div > div[data-testid="stVerticalBlockBorderWrapper"] {
        height: 100%;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }
</style>
""", unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# 2. DATABASE HELPER FUNCTIONS (WITH CACHING & ERROR HANDLING)
# -----------------------------------------------------------------------------
DB_PATH = DEFAULT_DB_PATH

# 24x7 Three-Hour Time Windows Definition
TIME_WINDOWS_3HR = [
    {"index": 0, "label": "00:00 - 03:00", "period": "Late Night / Midnight", "icon": "🌙"},
    {"index": 1, "label": "03:00 - 06:00", "period": "Early Morning Twilight", "icon": "🌅"},
    {"index": 2, "label": "06:00 - 09:00", "period": "Morning Rush Hour", "icon": "☕"},
    {"index": 3, "label": "09:00 - 12:00", "period": "Mid-Day / Late Morning", "icon": "☀️"},
    {"index": 4, "label": "12:00 - 15:00", "period": "Afternoon Off-Peak", "icon": "🍽️"},
    {"index": 5, "label": "15:00 - 18:00", "period": "Late Afternoon Surge", "icon": "🌇"},
    {"index": 6, "label": "18:00 - 21:00", "period": "Prime Evening Commute", "icon": "🌆"},
    {"index": 7, "label": "21:00 - 24:00", "period": "Night Express Overnight", "icon": "🌌"},
]


def check_database_readiness(db_path: str = DB_PATH) -> bool:
    """Verifies that the SQLite database exists and contains valid schema."""
    if not os.path.exists(db_path):
        return False
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fare_snapshots';")
            return cursor.fetchone() is not None
    except Exception:
        return False


@st.cache_data(ttl=30)
def load_cities(db_path: str = DB_PATH) -> pd.DataFrame:
    """Loads all registered origin and destination cities."""
    with get_db_connection(db_path) as conn:
        df = pd.read_sql_query("SELECT city_id, city_name, state, country FROM cities ORDER BY city_name ASC;", conn)
    return df


@st.cache_data(ttl=30)
def load_operators(db_path: str = DB_PATH) -> pd.DataFrame:
    """Loads all discovered bus operators."""
    with get_db_connection(db_path) as conn:
        df = pd.read_sql_query("""
            SELECT 
                operator_id, 
                operator_name, 
                is_rtc, 
                is_primo, 
                max_rating, 
                rating_count, 
                updated_at 
            FROM operators 
            ORDER BY rating_count DESC, max_rating DESC, operator_name ASC;
        """, conn)
    return df


@st.cache_data(ttl=30)
def load_available_routes(db_path: str = DB_PATH) -> pd.DataFrame:
    """Retrieves unique routes (from_city_id, to_city_id, travel_date) currently scraped."""
    with get_db_connection(db_path) as conn:
        query = """
            SELECT DISTINCT 
                s.from_city_id,
                COALESCE(fc.city_name, 'City ' || s.from_city_id) AS from_city_name,
                s.to_city_id,
                COALESCE(tc.city_name, 'City ' || s.to_city_id) AS to_city_name,
                s.travel_date,
                COUNT(s.service_id) AS bus_count,
                MAX(s.scraped_at) AS last_scraped_at
            FROM fare_snapshots s
            LEFT JOIN cities fc ON s.from_city_id = fc.city_id
            LEFT JOIN cities tc ON s.to_city_id = tc.city_id
            GROUP BY s.from_city_id, s.to_city_id, s.travel_date
            ORDER BY bus_count DESC;
        """
        df = pd.read_sql_query(query, conn)
    return df


def get_last_scraped_at(from_id: int, to_id: int, doj: Optional[str] = None, db_path: str = DB_PATH) -> Optional[str]:
    """Retrieves the timestamp of the latest scrape for a route."""
    try:
        with get_db_connection(db_path) as conn:
            q = "SELECT MAX(scraped_at) FROM fare_snapshots WHERE from_city_id = ? AND to_city_id = ?"
            params = [from_id, to_id]
            if doj:
                q += " AND travel_date = ?"
                params.append(doj)
            row = conn.execute(q, params).fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


@st.cache_data(ttl=30)
def load_route_operators(from_id: int, to_id: int, doj: Optional[str] = None, db_path: str = DB_PATH) -> pd.DataFrame:
    """Loads distinct operators offering services on a specific route."""
    with get_db_connection(db_path) as conn:
        query = """
            SELECT DISTINCT 
                s.operator_id,
                s.operator_name,
                o.is_rtc,
                o.is_primo,
                COUNT(s.service_id) as route_services,
                ROUND(MIN(s.effective_fare), 2) as min_fare,
                ROUND(MAX(s.effective_fare), 2) as max_fare
            FROM fare_snapshots s
            LEFT JOIN operators o ON s.operator_id = o.operator_id
            WHERE s.from_city_id = ? AND s.to_city_id = ?
        """
        params = [from_id, to_id]
        if doj:
            query += " AND s.travel_date = ?"
            params.append(doj)
        query += " GROUP BY s.operator_id, s.operator_name ORDER BY route_services DESC, s.operator_name ASC;"
        df = pd.read_sql_query(query, conn, params=params)
    return df


@st.cache_data(ttl=30)
def load_fare_snapshots(
    from_id: int,
    to_id: int,
    doj: Optional[str] = None,
    operator_ids: Optional[List[int]] = None,
    db_path: str = DB_PATH
) -> pd.DataFrame:
    """Loads granular bus service fare snapshots with optional competitor filters."""
    with get_db_connection(db_path) as conn:
        query = """
            SELECT 
                s.snapshot_id,
                s.scraped_at,
                s.hours_to_departure,
                s.from_city_id,
                fc.city_name AS from_city_name,
                s.to_city_id,
                tc.city_name AS to_city_name,
                s.travel_date,
                s.operator_id,
                s.operator_name,
                o.is_rtc,
                o.is_primo,
                s.service_id,
                s.bus_type,
                s.is_ac,
                s.is_sleeper,
                s.departure_time,
                s.arrival_time,
                s.duration,
                s.original_fare,
                s.effective_fare,
                s.discount_pct,
                s.total_seats,
                s.seats_available,
                s.single_seats_available,
                s.rating,
                s.review_count,
                s.fare_tiers_json,
                s.boarding_points_json,
                s.dropping_points_json,
                s.deal_tag
            FROM fare_snapshots s
            LEFT JOIN cities fc ON s.from_city_id = fc.city_id
            LEFT JOIN cities tc ON s.to_city_id = tc.city_id
            LEFT JOIN operators o ON s.operator_id = o.operator_id
            WHERE s.from_city_id = ? AND s.to_city_id = ?
        """
        params: List[Any] = [from_id, to_id]

        if doj:
            query += " AND s.travel_date = ?"
            params.append(doj)

        if operator_ids:
            placeholders = ",".join("?" for _ in operator_ids)
            query += f" AND s.operator_id IN ({placeholders})"
            params.extend(operator_ids)

        query += " ORDER BY s.effective_fare ASC, s.departure_time ASC;"
        df = pd.read_sql_query(query, conn, params=params)
    return df


def clear_app_cache():
    """Invalidates cached queries when database mutations occur."""
    st.cache_data.clear()


# -----------------------------------------------------------------------------
# 3. LIVE SCRAPING & DATA REFRESH ENGINE
# -----------------------------------------------------------------------------
def execute_live_scrape_process(
    from_id: int,
    from_name: str,
    to_id: int,
    to_name: str,
    doj: str,
    days_ahead: Optional[str] = None,
    db_path: str = DB_PATH
) -> subprocess.Popen:
    """Spawns the main.py CLI scraper as an isolated subprocess and streams stdout."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(script_dir, "main.py")

    cmd = [
        sys.executable,
        main_script,
        "--from-id", str(from_id),
        "--from-name", str(from_name),
        "--to-id", str(to_id),
        "--to-name", str(to_name),
        "--date", str(doj),
        "--db-path", db_path,
        "--headless"
    ]
    if days_ahead:
        cmd.extend(["--days-ahead", str(days_ahead)])

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )


def run_live_scrape_ui(
    routes_to_scrape: List[Tuple[int, str, int, str, str]],
    days_ahead: Optional[str] = None
):
    """
    Executes live scraping with real-time UI logging and progress feedback.
    Upon completion, invalidates cache and triggers st.rerun().
    """
    total_routes = len(routes_to_scrape)
    with st.status(
        f"⚡ Automatically fetching live data from redBus ({total_routes} route{'s' if total_routes > 1 else ''})...",
        expanded=True
    ) as status:
        for idx, (f_id, f_name, t_id, t_name, doj) in enumerate(routes_to_scrape, 1):
            st.markdown(f"🚍 **Route {idx}/{total_routes}:** `{f_name} (ID: {f_id})` $\\rightarrow$ `{t_name} (ID: {t_id})` | Date: `{doj}`")
            proc = execute_live_scrape_process(f_id, f_name, t_id, t_name, doj, days_ahead)

            log_box = st.empty()
            recent_lines: List[str] = []
            
            if proc.stdout:
                for line in iter(proc.stdout.readline, ''):
                    clean_line = line.strip()
                    if clean_line:
                        recent_lines.append(clean_line)
                        # Keep display focused on the most recent 3-4 log events
                        tail_snippet = "\n".join(recent_lines[-4:])
                        log_box.code(tail_snippet, language="text")

                proc.stdout.close()
            code = proc.wait()

            if code == 0:
                st.write(f"✅ Successfully ingested live listings for {f_name} $\\rightarrow$ {t_name}!")
            else:
                st.warning(f"⚠️ Scraping completed with exit code {code} for {f_name} $\\rightarrow$ {t_name}. Proceeding...")

        status.update(label="🎉 Fresh dynamic pricing data fetched and saved to SQLite!", state="complete", expanded=False)

    clear_app_cache()
    st.toast("✅ Real-time data successfully refreshed!")
    st.rerun()


# -----------------------------------------------------------------------------
# 4. EXCEL EXPORT HELPER (MULTI-TAB PROFESSIONAL SPREADSHEET)
# -----------------------------------------------------------------------------
def generate_excel_report(
    snapshots_df: pd.DataFrame,
    from_name: str,
    to_name: str,
    travel_date: str
) -> bytes:
    """Generates a professional, multi-tab Excel workbook using openpyxl."""
    wb = Workbook()
    
    # Styles
    navy_header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    red_accent_fill = PatternFill(start_color="D84E55", end_color="D84E55", fill_type="solid")
    light_gray_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
    
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    bold_font = Font(name="Segoe UI", size=11, bold=True)
    regular_font = Font(name="Segoe UI", size=10)
    title_font = Font(name="Segoe UI", size=16, bold=True, color="1E3A8A")
    subtitle_font = Font(name="Segoe UI", size=11, italic=True, color="6B7280")
    
    border_thin = Border(
        left=Side(style='thin', color='E5E7EB'),
        right=Side(style='thin', color='E5E7EB'),
        top=Side(style='thin', color='E5E7EB'),
        bottom=Side(style='thin', color='E5E7EB')
    )

    # Sheet 1: Executive Summary & KPI Metrics
    ws_summary = wb.active
    ws_summary.title = "Executive Summary"
    ws_summary.views.sheetView[0].showGridLines = True

    ws_summary["A2"] = "Competitor Dynamic Pricing & Market Intelligence Report"
    ws_summary["A2"].font = title_font
    ws_summary["A3"] = f"Route: {from_name} -> {to_name} | Journey Date: {travel_date} | Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ws_summary["A3"].font = subtitle_font

    total_buses = len(snapshots_df)
    unique_ops = snapshots_df["operator_id"].nunique() if not snapshots_df.empty else 0
    min_fare = snapshots_df["effective_fare"].min() if not snapshots_df.empty else 0
    median_fare = snapshots_df["effective_fare"].median() if not snapshots_df.empty else 0
    max_fare = snapshots_df["effective_fare"].max() if not snapshots_df.empty else 0
    avg_discount = snapshots_df["discount_pct"].mean() if not snapshots_df.empty else 0
    total_seats = snapshots_df["seats_available"].sum() if not snapshots_df.empty else 0
    total_reviews = snapshots_df["review_count"].sum() if not snapshots_df.empty else 0

    metrics_data = [
        ("Route Origin", from_name),
        ("Route Destination", to_name),
        ("Date of Journey (DOJ)", travel_date),
        ("Total Operators Discovered", unique_ops),
        ("Total Bus Services Scraped", total_buses),
        ("Market Minimum Effective Fare", f"₹{int(min_fare):,}"),
        ("Market Median Dynamic Fare", f"₹{int(median_fare):,}"),
        ("Market Maximum Fare", f"₹{int(max_fare):,}"),
        ("Average Market Discount %", f"{avg_discount:.1f}%"),
        ("Total Remaining Seats Tracked", f"{int(total_seats):,}"),
        ("Total User Ratings / Reviews", f"{int(total_reviews):,}"),
    ]

    ws_summary["A5"] = "Market Metric"
    ws_summary["B5"] = "Benchmark Value"
    ws_summary["A5"].font = header_font
    ws_summary["B5"].font = header_font
    ws_summary["A5"].fill = navy_header_fill
    ws_summary["B5"].fill = navy_header_fill

    for idx, (label, val) in enumerate(metrics_data, start=6):
        ws_summary[f"A{idx}"] = label
        ws_summary[f"B{idx}"] = val
        ws_summary[f"A{idx}"].font = bold_font
        ws_summary[f"B{idx}"].font = regular_font
        ws_summary[f"A{idx}"].border = border_thin
        ws_summary[f"B{idx}"].border = border_thin
        if idx % 2 == 0:
            ws_summary[f"A{idx}"].fill = light_gray_fill
            ws_summary[f"B{idx}"].fill = light_gray_fill

    ws_summary.column_dimensions["A"].width = 35
    ws_summary.column_dimensions["B"].width = 25

    # Sheet 2: Operator Benchmarking
    ws_ops = wb.create_sheet(title="Operator Benchmarking")
    ws_ops.views.sheetView[0].showGridLines = True

    if not snapshots_df.empty:
        op_agg = snapshots_df.groupby(["operator_id", "operator_name", "is_rtc", "is_primo"]).agg(
            total_buses=("service_id", "count"),
            min_fare=("effective_fare", "min"),
            median_fare=("effective_fare", "median"),
            max_fare=("effective_fare", "max"),
            avg_discount=("discount_pct", "mean"),
            total_seats=("seats_available", "sum"),
            avg_rating=("rating", "mean"),
            total_reviews=("review_count", "sum")
        ).reset_index().sort_values(by=["total_buses", "min_fare"], ascending=[False, True])

        op_headers = [
            "Operator ID", "Operator Name", "Category", "Primo Verified",
            "Buses", "Min Fare (₹)", "Median Fare (₹)", "Max Fare (₹)",
            "Avg Disc %", "Seats Avail", "Avg Rating", "Review Count"
        ]

        for col_num, h in enumerate(op_headers, start=1):
            cell = ws_ops.cell(row=1, column=col_num, value=h)
            cell.font = header_font
            cell.fill = navy_header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for row_idx, r in enumerate(op_agg.itertuples(), start=2):
            category = "RTC Government" if r.is_rtc == 1 else "Private"
            primo = "Yes" if r.is_primo == 1 else "No"
            row_vals = [
                r.operator_id,
                r.operator_name,
                category,
                primo,
                r.total_buses,
                r.min_fare,
                r.median_fare,
                r.max_fare,
                round(r.avg_discount, 1),
                r.total_seats,
                round(r.avg_rating, 1) if pd.notnull(r.avg_rating) else "N/A",
                r.total_reviews
            ]
            for col_idx, v in enumerate(row_vals, start=1):
                cell = ws_ops.cell(row=row_idx, column=col_idx, value=v)
                cell.font = regular_font
                cell.border = border_thin
                if col_idx in [6, 7, 8]:
                    cell.number_format = '₹#,##0'
                elif col_idx == 9:
                    cell.number_format = '0.0"%"'
                if row_idx % 2 == 0:
                    cell.fill = light_gray_fill

        for col in ws_ops.columns:
            max_len = max(len(str(c.value or '')) for c in col)
            col_letter = get_column_letter(col[0].column)
            ws_ops.column_dimensions[col_letter].width = max(max_len + 4, 12)

    # Sheet 3: Granular Bus Service Listings
    ws_buses = wb.create_sheet(title="All Bus Services")
    ws_buses.views.sheetView[0].showGridLines = True

    bus_export_cols = [
        "service_id", "operator_name", "bus_type", "departure_time", "arrival_time",
        "duration", "effective_fare", "original_fare", "discount_pct",
        "seats_available", "single_seats_available", "rating", "review_count",
        "deal_tag", "is_ac", "is_sleeper"
    ]
    
    bus_headers = [
        "Service ID", "Operator Name", "Bus Type", "Departure", "Arrival",
        "Duration", "Effective Fare (₹)", "Original Fare (₹)", "Discount %",
        "Seats Available", "Single Seats", "Rating (★)", "Reviews",
        "Deal Tag", "AC Coach", "Sleeper Coach"
    ]

    for col_num, h in enumerate(bus_headers, start=1):
        cell = ws_buses.cell(row=1, column=col_num, value=h)
        cell.font = header_font
        cell.fill = red_accent_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    if not snapshots_df.empty:
        for row_idx, r in enumerate(snapshots_df[bus_export_cols].itertuples(index=False), start=2):
            ac_str = "Yes" if getattr(r, "is_ac", 0) == 1 else "No"
            slp_str = "Yes" if getattr(r, "is_sleeper", 0) == 1 else "No"
            row_data = list(r)
            row_data[-2] = ac_str
            row_data[-1] = slp_str

            for col_idx, v in enumerate(row_data, start=1):
                cell = ws_buses.cell(row=row_idx, column=col_idx, value=v if pd.notnull(v) else "")
                cell.font = regular_font
                cell.border = border_thin
                if col_idx in [7, 8] and isinstance(v, (int, float)):
                    cell.number_format = '₹#,##0'
                elif col_idx == 9 and isinstance(v, (int, float)):
                    cell.number_format = '0.0"%"'
                if row_idx % 2 == 0:
                    cell.fill = light_gray_fill

        for col in ws_buses.columns:
            max_len = max(len(str(c.value or '')) for c in col)
            col_letter = get_column_letter(col[0].column)
            ws_buses.column_dimensions[col_letter].width = max(min(max_len + 4, 40), 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


# -----------------------------------------------------------------------------
# 5. AUTHENTICATION & ACCESS CONTROL GATEWAY
# -----------------------------------------------------------------------------
if not is_authenticated():
    render_login_screen()
    st.stop()


# -----------------------------------------------------------------------------
# 6. MAIN APP LAYOUT & ROUTING (AUTHENTICATED ONLY)
# -----------------------------------------------------------------------------

# Render Active User Badge and Sign-Out Button at top of sidebar
render_sidebar_auth_widget()
st.sidebar.markdown("---")

# Check database existence and auto-initialize schema & seed cities if needed
if not check_database_readiness(DB_PATH):
    init_db(DB_PATH)
    upsert_city(733, "Delhi", "Delhi", "India", DB_PATH)
    upsert_city(807, "Jaipur (Rajasthan)", "Rajasthan", "India", DB_PATH)
    upsert_city(1290, "Agra", "Uttar Pradesh", "India", DB_PATH)

# Pre-load dimension tables
cities_df = load_cities(DB_PATH)
routes_df = load_available_routes(DB_PATH)

if cities_df.empty:
    upsert_city(733, "Delhi", "Delhi", "India", DB_PATH)
    upsert_city(807, "Jaipur (Rajasthan)", "Rajasthan", "India", DB_PATH)
    upsert_city(1290, "Agra", "Uttar Pradesh", "India", DB_PATH)
    cities_df = load_cities(DB_PATH)

# City mapping
city_options = dict(zip(cities_df["city_name"], cities_df["city_id"]))
city_names = list(city_options.keys())
if not city_names:
    city_names = ["Delhi", "Jaipur (Rajasthan)", "Agra"]
    city_options = {"Delhi": 733, "Jaipur (Rajasthan)": 807, "Agra": 1290}

# Default Origin: Delhi (733)
default_origin_idx = 0
for idx, name in enumerate(city_names):
    if "delhi" in name.lower():
        default_origin_idx = idx
        break

# Default Destination: Jaipur (807) or Agra (1290)
default_dest_idx = min(1, len(city_names) - 1)
for idx, name in enumerate(city_names):
    if "jaipur" in name.lower() or "agra" in name.lower():
        default_dest_idx = idx
        break

# Sidebar: Route Selector
st.sidebar.header("🔍 Route & Competitor Filter")
selected_origin_name = st.sidebar.selectbox("Origin City (From)", options=city_names, index=default_origin_idx)
selected_origin_id = city_options.get(selected_origin_name, 733)

dest_candidates = [c for c in city_names if c != selected_origin_name]
if not dest_candidates:
    dest_candidates = [c for c in city_names]
dest_default_idx = 0
if len(dest_candidates) > 1 and default_dest_idx < len(dest_candidates):
    dest_default_idx = default_dest_idx

selected_dest_name = st.sidebar.selectbox("Destination City (To)", options=dest_candidates, index=dest_default_idx)
selected_dest_id = city_options.get(selected_dest_name, 807)

# Check existing dates in DB
matching_routes = routes_df[
    (routes_df["from_city_id"] == selected_origin_id) & 
    (routes_df["to_city_id"] == selected_dest_id)
]
available_dates = matching_routes["travel_date"].unique().tolist() if not matching_routes.empty else []

st.sidebar.markdown("---")
st.sidebar.subheader("📅 Travel Date (DOJ)")

# Determine default date for calendar
default_date = datetime(2026, 10, 27).date()
if available_dates:
    try:
        default_date = datetime.strptime(available_dates[0], "%d-%b-%Y").date()
    except Exception:
        pass

if "calendar_doj_val" not in st.session_state:
    st.session_state["calendar_doj_val"] = default_date

def set_quick_doj(d_val):
    st.session_state["calendar_doj_val"] = d_val

if available_dates:
    date_mode = st.sidebar.radio(
        "Date Selection Method:",
        ["📅 Pick Any Date (Calendar)", f"💾 Saved in Database ({len(available_dates)})"],
        index=0,
        horizontal=True,
        help="Select 'Pick Any Date' to choose any calendar date (past, today, tomorrow, or future) to fetch live data, or choose from previously saved dates in the database."
    )

    if date_mode.startswith("💾"):
        selected_doj = st.sidebar.selectbox(
            "Select Previously Scraped Date:",
            options=available_dates,
            index=0,
            help="Dates currently saved in the database for this route."
        )
    else:
        # Quick preset buttons for convenience: Today, Tomorrow, +2 Days, +7 Days
        p_c1, p_c2, p_c3, p_c4 = st.sidebar.columns(4)
        base_d = datetime(2026, 10, 27).date()
        with p_c1:
            st.button("27-Oct", on_click=set_quick_doj, args=(base_d,), use_container_width=True, help="Today: 27-Oct-2026")
        with p_c2:
            st.button("28-Oct", on_click=set_quick_doj, args=(base_d + timedelta(days=1),), use_container_width=True, help="Tomorrow: 28-Oct-2026")
        with p_c3:
            st.button("29-Oct", on_click=set_quick_doj, args=(base_d + timedelta(days=2),), use_container_width=True, help="+2 Days: 29-Oct-2026")
        with p_c4:
            st.button("03-Nov", on_click=set_quick_doj, args=(base_d + timedelta(days=7),), use_container_width=True, help="+7 Days: 03-Nov-2026")

        selected_doj_picker = st.sidebar.date_input(
            "Choose Journey Date:",
            value=st.session_state["calendar_doj_val"],
            help="Select any date from the calendar. If data doesn't exist yet, click 'Refresh & Fetch Live Data' to scrape live."
        )
        st.session_state["calendar_doj_val"] = selected_doj_picker
        selected_doj = selected_doj_picker.strftime("%d-%b-%Y")
else:
    st.sidebar.info("💡 No historical data found for this route. Pick a date below to scrape live redBus listings:")

    p_c1, p_c2, p_c3, p_c4 = st.sidebar.columns(4)
    base_d = datetime(2026, 10, 27).date()
    with p_c1:
        st.button("27-Oct", on_click=set_quick_doj, args=(base_d,), use_container_width=True, help="27-Oct-2026")
    with p_c2:
        st.button("28-Oct", on_click=set_quick_doj, args=(base_d + timedelta(days=1),), use_container_width=True, help="28-Oct-2026")
    with p_c3:
        st.button("29-Oct", on_click=set_quick_doj, args=(base_d + timedelta(days=2),), use_container_width=True, help="29-Oct-2026")
    with p_c4:
        st.button("03-Nov", on_click=set_quick_doj, args=(base_d + timedelta(days=7),), use_container_width=True, help="03-Nov-2026")

    selected_doj_picker = st.sidebar.date_input(
        "Choose Journey Date:",
        value=st.session_state["calendar_doj_val"],
        help="Select any travel date from the calendar to fetch live redBus data."
    )
    st.session_state["calendar_doj_val"] = selected_doj_picker
    selected_doj = selected_doj_picker.strftime("%d-%b-%Y")

# Display clear database status indicator
is_in_db = selected_doj in available_dates
if is_in_db:
    bus_cnt_date = matching_routes[matching_routes["travel_date"] == selected_doj]["bus_count"].sum() if not matching_routes.empty else 0
    st.sidebar.caption(f"🟢 **Saved in Database:** `{int(bus_cnt_date)}` bus listings recorded")
else:
    st.sidebar.caption(f"⚡ **New Date (`{selected_doj}`):** Ready to fetch live from redBus")

# Last scraped status
last_scraped_raw = get_last_scraped_at(selected_origin_id, selected_dest_id, selected_doj, DB_PATH)
if last_scraped_raw:
    last_scraped_display = last_scraped_raw[:19].replace("T", " ") + " UTC"
    status_card_html = f'<div style="font-size: 0.75rem; color: #10B981; margin-top: 2px;">🕒 Last Scraped: {last_scraped_display}</div>'
else:
    status_card_html = '<div style="font-size: 0.75rem; color: #D84E55; margin-top: 2px; font-weight:600;">⚡ Not in DB yet — Click button below to fetch live!</div>'

# -----------------------------------------------------------------------------
# LIVE DATA FETCH SECTION IN SIDEBAR
# -----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.subheader("⚡ Live Data Fetch")
st.sidebar.markdown(f"""
<div class="scrape-card">
  <div style="font-size: 0.8rem; color: #888;">ROUTE:</div>
  <b>{selected_origin_name} ➔ {selected_dest_name}</b><br/>
  <div style="font-size: 0.8rem; color: #888; margin-top: 4px;">JOURNEY DATE: <code>{selected_doj}</code></div>
  {status_card_html}
</div>
""", unsafe_allow_html=True)

sidebar_forward_curve = st.sidebar.checkbox("Scrape Forward Curve (+1, +3 days)", value=False)
sidebar_all_routes = st.sidebar.checkbox("Scrape All Registered Routes", value=False)

sidebar_fetch_clicked = st.sidebar.button(
    "🚀 Fetch Latest redBus Data",
    use_container_width=True,
    help="Spawns Playwright to scrape live dynamic fares, load factors, and competitor discounts"
)

# -----------------------------------------------------------------------------
# TOP HEADER & MAIN REFRESH BUTTON
# -----------------------------------------------------------------------------
col_h1, col_h2 = st.columns([3, 2])
with col_h1:
    st.markdown('<div class="brand-title">🚌 Bus Pricing Intelligence & Competitor Dashboard</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="brand-subtitle">Real-time competitor dynamic pricing, load factor intelligence, and route benchmarking on redBus | <b>{selected_origin_name} ➔ {selected_dest_name}</b> ({selected_doj})</div>', unsafe_allow_html=True)

with col_h2:
    col_btn1, col_btn2 = st.columns([3, 2])
    with col_btn1:
        header_fetch_clicked = st.button(
            "⚡ Refresh & Fetch Live Data",
            type="primary",
            use_container_width=True,
            help="Connects directly to redBus, scrapes all new bus listings & dynamic fares, and updates SQLite"
        )
    with col_btn2:
        cache_reload_clicked = st.button(
            "🔄 Reload Cache",
            use_container_width=True,
            help="Clears Streamlit cache to re-read current SQLite records without rescraping"
        )

# Handle Cache Reload Only
if cache_reload_clicked:
    clear_app_cache()
    st.success("Cache cleared! Reloaded current database records.")
    st.rerun()

# Handle Live Data Fetch Request (From Header or Sidebar)
if header_fetch_clicked or sidebar_fetch_clicked:
    days_arg = "0,1,3" if sidebar_forward_curve else None
    
    if sidebar_all_routes:
        # Build list of all distinct routes in database
        routes_to_fetch = []
        for _, r in routes_df.iterrows():
            routes_to_fetch.append((int(r["from_city_id"]), str(r["from_city_name"]), int(r["to_city_id"]), str(r["to_city_name"]), str(r["travel_date"])))
        if not routes_to_fetch:
            routes_to_fetch = [(selected_origin_id, selected_origin_name, selected_dest_id, selected_dest_name, selected_doj)]
    else:
        routes_to_fetch = [(selected_origin_id, selected_origin_name, selected_dest_id, selected_dest_name, selected_doj)]

    run_live_scrape_ui(routes_to_fetch, days_ahead=days_arg)


# -----------------------------------------------------------------------------
# SIDEBAR COMPETITOR & FEATURE FILTERS
# -----------------------------------------------------------------------------
route_ops_df = load_route_operators(selected_origin_id, selected_dest_id, selected_doj, DB_PATH)

st.sidebar.markdown("---")
st.sidebar.subheader("🏢 Competitor Selection")

if not route_ops_df.empty:
    op_list = route_ops_df["operator_name"].tolist()
    op_id_map = dict(zip(route_ops_df["operator_name"], route_ops_df["operator_id"]))

    select_all_ops = st.sidebar.checkbox("Select All Competitors", value=True)
    if select_all_ops:
        selected_op_names = st.sidebar.multiselect(
            "Filter Specific Operators",
            options=op_list,
            default=op_list,
            disabled=True
        )
        selected_op_ids = [op_id_map[name] for name in op_list]
    else:
        selected_op_names = st.sidebar.multiselect(
            "Filter Specific Operators",
            options=op_list,
            default=op_list[:min(5, len(op_list))]
        )
        selected_op_ids = [op_id_map[name] for name in selected_op_names]
else:
    selected_op_ids = None

st.sidebar.markdown("---")
st.sidebar.subheader("🎛️ Coach & Dynamic Filters")
coach_filter = st.sidebar.radio("Coach Comfort Tier", ["All", "AC Only", "Non-AC", "Sleeper Only", "Seater Only"])
min_rating_filter = st.sidebar.slider("Minimum Rating (★)", 0.0, 5.0, 0.0, step=0.1)


# Load Snapshots for Active Selection
snapshots_df = load_fare_snapshots(
    from_id=selected_origin_id,
    to_id=selected_dest_id,
    doj=selected_doj,
    operator_ids=selected_op_ids,
    db_path=DB_PATH
)

# Apply Granular Filters
if not snapshots_df.empty:
    if coach_filter == "AC Only":
        snapshots_df = snapshots_df[snapshots_df["is_ac"] == 1]
    elif coach_filter == "Non-AC":
        snapshots_df = snapshots_df[snapshots_df["is_ac"] == 0]
    elif coach_filter == "Sleeper Only":
        snapshots_df = snapshots_df[snapshots_df["is_sleeper"] == 1]
    elif coach_filter == "Seater Only":
        snapshots_df = snapshots_df[snapshots_df["is_sleeper"] == 0]

    if min_rating_filter > 0:
        snapshots_df = snapshots_df[snapshots_df["rating"] >= min_rating_filter]


# -----------------------------------------------------------------------------
# DASHBOARD TABS
# -----------------------------------------------------------------------------
tab_explorer, tab_3hr_compare, tab_analytics, tab_directory, tab_export = st.tabs([
    "🔍 Multi-Operator Explorer",
    "⏱️ 24×7 3-Hour Price Matrix",
    "📊 Dynamic Pricing Analytics",
    "🏙️ Master Directory",
    "📥 Excel Export Center"
])


# =============================================================================
# TAB 1: 🔍 MULTI-OPERATOR ROUTE & DATE EXPLORER
# =============================================================================
with tab_explorer:
    st.markdown(f"### 📍 Route: **{selected_origin_name}** $\\rightarrow$ **{selected_dest_name}** | Journey Date: `{selected_doj}`")

    if snapshots_df.empty:
        with st.container(border=True):
            st.markdown(f"#### 🛰️ No live records in database for **{selected_origin_name} ➔ {selected_dest_name}** on `{selected_doj}`")
            st.caption(f"You selected journey date **`{selected_doj}`**. Click below to connect live to redBus, capture all operator prices, discounts, and load factors into SQLite:")
            col_scrape_prompt, _ = st.columns([2, 3])
            with col_scrape_prompt:
                if st.button(f"🚀 Fetch Live redBus Data for {selected_doj} Now", type="primary", use_container_width=True):
                    run_live_scrape_ui([(selected_origin_id, selected_origin_name, selected_dest_id, selected_dest_name, selected_doj)])
    else:
        # Top KPI Metrics Cards
        kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
        
        total_matching_buses = len(snapshots_df)
        lowest_fare = snapshots_df["effective_fare"].min()
        median_fare = snapshots_df["effective_fare"].median()
        avg_rating = snapshots_df["rating"].mean()
        total_avail_seats = snapshots_df["seats_available"].sum()

        with kpi1:
            st.metric("🚍 Matching Buses", f"{total_matching_buses:,}")
        with kpi2:
            st.metric("🏷️ Lowest Fare", f"₹{int(lowest_fare):,}")
        with kpi3:
            st.metric("⚖️ Median Fare", f"₹{int(median_fare):,}")
        with kpi4:
            st.metric("⭐ Avg Rating", f"{avg_rating:.1f} ★" if pd.notnull(avg_rating) else "N/A")
        with kpi5:
            st.metric("💺 Available Seats", f"{int(total_avail_seats):,}")

        st.markdown("---")

        # Table Display
        st.subheader("📋 Real-Time Bus & Dynamic Pricing Comparison Table")
        
        display_cols = [
            "operator_name", "is_rtc", "is_primo", "bus_type", "departure_time",
            "arrival_time", "duration", "effective_fare", "original_fare",
            "discount_pct", "seats_available", "single_seats_available",
            "rating", "review_count", "deal_tag"
        ]
        
        table_df = snapshots_df[display_cols].copy()
        table_df["Category"] = table_df["is_rtc"].apply(lambda x: "RTC Gov" if x == 1 else "Private")
        table_df["Primo"] = table_df["is_primo"].apply(lambda x: "⭐ Primo" if x == 1 else "Standard")
        table_df["Effective Fare"] = table_df["effective_fare"].apply(lambda x: f"₹{int(x):,}")
        table_df["Original Fare"] = table_df["original_fare"].apply(lambda x: f"₹{int(x):,}" if pd.notnull(x) and x > 0 else "-")
        table_df["Discount %"] = table_df["discount_pct"].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) and x > 0 else "0.0%")
        table_df["Rating (★)"] = table_df["rating"].apply(lambda x: f"{x:.1f} ★" if pd.notnull(x) else "N/A")
        table_df["Reviews"] = table_df["review_count"].apply(lambda x: f"{int(x):,}" if pd.notnull(x) and x > 0 else "-")
        table_df["Deal Tag"] = table_df["deal_tag"].fillna("-")

        final_table = table_df[[
            "operator_name", "Category", "Primo", "bus_type", "departure_time",
            "arrival_time", "duration", "Effective Fare", "Original Fare",
            "Discount %", "seats_available", "Rating (★)", "Reviews", "Deal Tag"
        ]].rename(columns={
            "operator_name": "Operator Name",
            "bus_type": "Bus Coach Type",
            "departure_time": "Dep Time",
            "arrival_time": "Arr Time",
            "duration": "Duration",
            "seats_available": "Seats Left",
        })

        st.dataframe(
            final_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Operator Name": st.column_config.TextColumn("Operator", width="medium"),
                "Effective Fare": st.column_config.TextColumn("Dynamic Fare", width="small"),
                "Seats Left": st.column_config.NumberColumn("Seats", width="small"),
            }
        )

        with st.expander("📍 View Boarding & Dropping Points Intelligence"):
            st.write("Unique pickup locations discovered across operators:")
            all_boarding = []
            for bp_raw in snapshots_df["boarding_points_json"].dropna():
                try:
                    points = json.loads(bp_raw)
                    for p in points:
                        if isinstance(p, dict) and p.get("location"):
                            all_boarding.append(p["location"])
                except Exception:
                    pass
            
            if all_boarding:
                bp_series = pd.Series(all_boarding).value_counts().head(10)
                st.bar_chart(bp_series)
            else:
                st.caption("No boarding point breakdown found for current view.")


# =============================================================================
# TAB 2: ⏱️ 24×7 3-HOUR PRICE COMPARISON MATRIX
# =============================================================================
with tab_3hr_compare:
    st.markdown("### ⏱️ 24×7 Intra-Day 3-Hour Dynamic Price Matrix")
    st.markdown(f'<div class="brand-subtitle">Continuous 24-hour time-of-day pricing comparison segmented into eight 3-hour departure windows for <b>{selected_origin_name} ➔ {selected_dest_name}</b> ({selected_doj})</div>', unsafe_allow_html=True)

    if snapshots_df.empty:
        st.info("No bus listings available for the active route to perform 3-hour comparisons. Please select an active route or click 'Refresh & Fetch Live Data'.")
    else:
        # 1. Enrich DataFrame with 3-Hour Window Information
        df_3hr = snapshots_df.copy()

        def map_window_tuple(dep_str):
            if not dep_str or not isinstance(dep_str, str) or ":" not in dep_str:
                return (-1, "Unknown", "Unknown", "❓")
            try:
                parts = dep_str.strip().split(":")
                hour = int(parts[0])
                idx = min(7, max(0, hour // 3))
                w = TIME_WINDOWS_3HR[idx]
                return (idx, w["label"], w["period"], w["icon"])
            except Exception:
                return (-1, "Unknown", "Unknown", "❓")

        win_info = df_3hr["departure_time"].apply(map_window_tuple)
        df_3hr["win_idx"] = [w[0] for w in win_info]
        df_3hr["win_label"] = [w[1] for w in win_info]
        df_3hr["win_period"] = [w[2] for w in win_info]
        df_3hr["win_icon"] = [w[3] for w in win_info]
        df_3hr["win_full"] = [f"{w[3]} {w[1]}" for w in win_info]

        # Calculate Window-Level Aggregates
        win_stats = []
        for w in TIME_WINDOWS_3HR:
            sub = df_3hr[df_3hr["win_idx"] == w["index"]]
            if not sub.empty:
                min_f = sub["effective_fare"].min()
                med_f = sub["effective_fare"].median()
                max_f = sub["effective_fare"].max()
                buses = len(sub)
                ops = sub["operator_id"].nunique()
                seats = sub["seats_available"].sum()
                avg_rating = sub["rating"].mean()
            else:
                min_f = med_f = max_f = None
                buses = ops = seats = 0
                avg_rating = None

            win_stats.append({
                "index": w["index"],
                "label": w["label"],
                "period": w["period"],
                "icon": w["icon"],
                "buses": buses,
                "ops": ops,
                "min_fare": min_f,
                "median_fare": med_f,
                "max_fare": max_f,
                "seats": seats,
                "rating": avg_rating
            })

        win_stats_df = pd.DataFrame(win_stats)

        # Identify Best Value and Peak Surge Window
        valid_medians = win_stats_df.dropna(subset=["median_fare"])
        best_val_idx = int(valid_medians.loc[valid_medians["median_fare"].idxmin()]["index"]) if not valid_medians.empty else -1
        surge_idx = int(valid_medians.loc[valid_medians["median_fare"].idxmax()]["index"]) if not valid_medians.empty else -1

        # Extract all distinct operators on this route
        all_route_ops = sorted(df_3hr["operator_name"].dropna().unique().tolist())

        # ---------------------------------------------------------------------
        # Section 1: 24×7 CONTINUOUS 3-HOUR WINDOW SUMMARY STRIP (8 BLOCKS)
        # ---------------------------------------------------------------------
        st.markdown("#### 1. 24×7 Time-of-Day Departure Windows (3-Hour Blocks)")
        st.caption("Continuous market benchmark comparing fare minimums, medians, and surge peaks across the entire 24-hour operating cycle.")

        # Active Inspector Slot State (defaults to best value slot if available, else slot 2: 06:00-09:00)
        if "tab2_active_inspect_slot" not in st.session_state or st.session_state["tab2_active_inspect_slot"] is None:
            default_slot = int(best_val_idx) if best_val_idx >= 0 else 2
            st.session_state["tab2_active_inspect_slot"] = default_slot
        else:
            try:
                st.session_state["tab2_active_inspect_slot"] = int(st.session_state["tab2_active_inspect_slot"])
            except Exception:
                st.session_state["tab2_active_inspect_slot"] = 2

        # Toolbar Row (Well-proportioned columns to avoid clipping)
        sec1_tb_col1, sec1_tb_col2, sec1_tb_col3 = st.columns([2.4, 2.0, 1.8])
        with sec1_tb_col1:
            my_service_brand = st.selectbox(
                "🏷️ Designate 'My Service' Brand (Optional):",
                options=["-- None (Benchmark Competitors Only) --"] + all_route_ops,
                index=0,
                key="global_my_service_brand",
                help="Tags your selected bus brand as '⭐ My Service' across all slot cards and tabular matrices."
            )
        with sec1_tb_col2:
            sec1_layout_mode = st.radio(
                "📐 Slot Matrix Layout Mode:",
                options=["🖥️ Full-Width Inspector (Fit to Screen)", "📱 Inline Card Accordion"],
                index=0,
                horizontal=True,
                key="tab2_sec1_view_mode",
                help="Full-Width Inspector keeps all 8 cards uniform and displays the detailed matrix across the full screen width without distortion."
            )
        with sec1_tb_col3:
            slot_picker_options = ["❌ None (Summary Cards Only)"] + [f"{w['icon']} {w['label']} ({w['period']})" for w in TIME_WINDOWS_3HR]
            curr_slot_val = st.session_state.get("tab2_active_inspect_slot")
            if curr_slot_val is not None:
                try:
                    slot_int = int(curr_slot_val)
                    curr_picker_idx = int(slot_int + 1) if 0 <= slot_int < 8 else 0
                except Exception:
                    curr_picker_idx = 0
            else:
                curr_picker_idx = 0

            def on_quick_slot_change():
                val = st.session_state.get("quick_slot_selector_dropdown", "")
                if val.startswith("❌"):
                    st.session_state["tab2_active_inspect_slot"] = None
                else:
                    for w in TIME_WINDOWS_3HR:
                        if w["label"] in val:
                            st.session_state["tab2_active_inspect_slot"] = int(w["index"])
                            break

            st.selectbox(
                "⚡ Active Slot Inspector:",
                options=slot_picker_options,
                index=int(curr_picker_idx),
                key="quick_slot_selector_dropdown",
                on_change=on_quick_slot_change,
                help="Select any time window to open its Competitor Customizer and Enterprise Pricing Matrix"
            )

        is_my_brand_active = bool(my_service_brand and my_service_brand != "-- None (Benchmark Competitors Only) --")

        # Function to build enterprise table rows
        def build_slot_pricing_table(slot_services_df, is_my_service_active, brand_name):
            if slot_services_df.empty:
                return pd.DataFrame()
            table_rows = []
            for _, r in slot_services_df.iterrows():
                dep_str = str(r.get("departure_time", "—"))
                dep_clean = dep_str[:5] if ":" in dep_str else dep_str
                
                op_raw = str(r.get("operator_name", "—"))
                if is_my_service_active and op_raw.lower() == brand_name.lower():
                    op_display = f"⭐ My Service ({op_raw})"
                else:
                    op_display = op_raw
                
                b_type = str(r.get("bus_type", "Standard Coach"))
                fare_str = f"₹{int(r['effective_fare']):,}" if pd.notnull(r.get('effective_fare')) else "—"
                seats_cnt = int(r.get("seats_available", 0)) if pd.notnull(r.get("seats_available")) else 0
                
                table_rows.append({
                    "Time": dep_clean,
                    "Service (Operator Name)": op_display,
                    "Service Type (Bus/Coach model)": b_type,
                    "Fare (₹)": fare_str,
                    "Seats Left": seats_cnt
                })
            return pd.DataFrame(table_rows)

        # Helper callback to toggle slot inspector
        def toggle_slot_inspect(slot_id):
            slot_id = int(slot_id)
            curr = st.session_state.get("tab2_active_inspect_slot")
            if curr is not None and int(curr) == slot_id:
                st.session_state["tab2_active_inspect_slot"] = None
            else:
                st.session_state["tab2_active_inspect_slot"] = slot_id

        # Helper callback to set slot operators
        def set_slot_custom_ops(slot_id, ops_list):
            target_ops = [op for op in ops_list if op in all_route_ops]
            st.session_state[f"slot_custom_ops_{slot_id}"] = target_ops
            st.session_state[f"ms_inspector_slot_{slot_id}"] = target_ops
            st.session_state[f"ms_slot_{slot_id}"] = target_ops

        # Render Slot KPI Card
        def render_slot_card(w_data, container_col):
            slot_idx = w_data["index"]
            slot_df_full = df_3hr[df_3hr["win_idx"] == slot_idx]
            slot_avail_ops = sorted(slot_df_full["operator_name"].dropna().unique().tolist())
            
            # State key for this slot
            slot_key = f"slot_custom_ops_{slot_idx}"
            insp_ms_key = f"ms_inspector_slot_{slot_idx}"
            if insp_ms_key in st.session_state:
                current_selected = [op for op in st.session_state[insp_ms_key] if op in all_route_ops]
                st.session_state[slot_key] = current_selected
            elif slot_key in st.session_state:
                current_selected = [op for op in st.session_state[slot_key] if op in all_route_ops]
                st.session_state[insp_ms_key] = current_selected
            else:
                current_selected = slot_avail_ops
                st.session_state[slot_key] = slot_avail_ops
                st.session_state[insp_ms_key] = slot_avail_ops

            slot_filtered = slot_df_full[slot_df_full["operator_name"].isin(current_selected)]
            
            if not slot_filtered.empty:
                slot_med = slot_filtered["effective_fare"].median()
                slot_min = slot_filtered["effective_fare"].min()
                slot_max = slot_filtered["effective_fare"].max()
                slot_buses = len(slot_filtered)
                slot_ops = slot_filtered["operator_name"].nunique()
            else:
                slot_med = slot_min = slot_max = None
                slot_buses = slot_ops = 0

            curr_insp_val = st.session_state.get("tab2_active_inspect_slot")
            is_inspecting_this = (curr_insp_val is not None and int(curr_insp_val) == int(slot_idx))

            with container_col:
                with st.container(border=True):
                    badge_str = ""
                    if slot_idx == best_val_idx and slot_buses > 0:
                        badge_str = " :green[**[BEST VALUE]**]"
                    elif slot_idx == surge_idx and slot_buses > 0:
                        badge_str = " :red[**[PEAK SURGE]**]"

                    st.markdown(f"**{w_data['icon']} {w_data['label']}**{badge_str}")
                    st.caption(f"{w_data['period']}")
                    
                    med_val = f"₹{int(slot_med):,}" if slot_med is not None else "No Buses"
                    st.metric(label="Median Dynamic Fare", value=med_val)
                    range_val = f"₹{int(slot_min):,} - ₹{int(slot_max):,}" if slot_min is not None else "—"
                    st.caption(f"Range: **{range_val}** • 🚍 **{slot_buses}** buses · **{slot_ops}** ops")
                    
                    # Highlight 'My Service' if operating in this window
                    if is_my_brand_active:
                        my_buses = slot_df_full[slot_df_full["operator_name"] == my_service_brand]
                        if not my_buses.empty:
                            my_med = my_buses["effective_fare"].median()
                            if slot_med is not None:
                                delta_p = my_med - slot_med
                                sign = "+" if delta_p > 0 else ""
                                st.markdown(f"<small style='color:#10B981; font-weight:600;'>⭐ <b>My Service:</b> ₹{int(my_med):,} ({sign}₹{int(delta_p):,} vs Mkt)</small>", unsafe_allow_html=True)
                            else:
                                st.markdown(f"<small style='color:#10B981; font-weight:600;'>⭐ <b>My Service:</b> ₹{int(my_med):,}</small>", unsafe_allow_html=True)
                        else:
                            st.markdown("<small style='color:#6B7280;'>⭐ My Service: No departures</small>", unsafe_allow_html=True)

                    if sec1_layout_mode == "🖥️ Full-Width Inspector (Fit to Screen)":
                        # Clean uniform button that toggles the full-width inspector below
                        btn_label = "✅ Inspecting Below ▴" if is_inspecting_this else "➕ Add / Edit Competitors ▾"
                        btn_style = "primary" if is_inspecting_this else "secondary"
                        st.button(
                            btn_label,
                            key=f"btn_card_slot_{slot_idx}",
                            type=btn_style,
                            on_click=toggle_slot_inspect,
                            args=(slot_idx,),
                            use_container_width=True,
                            help=f"Inspect competitor breakdown and pricing matrix for {w_data['label']}"
                        )
                    else:
                        # Inline Card Accordion Mode
                        with st.expander("➕ Add or Edit Competitors", expanded=is_inspecting_this):
                            st.markdown(f"**Customization ({w_data['label']})**")
                            btn_c1, btn_c2, btn_c3 = st.columns(3)
                            with btn_c1:
                                st.button("All", key=f"btn_all_{slot_idx}", on_click=set_slot_custom_ops, args=(slot_idx, slot_avail_ops), use_container_width=True)
                            with btn_c2:
                                top3_ops = slot_df_full["operator_name"].value_counts().head(3).index.tolist()
                                st.button("Top 3", key=f"btn_top3_{slot_idx}", on_click=set_slot_custom_ops, args=(slot_idx, top3_ops), use_container_width=True)
                            with btn_c3:
                                st.button("Reset", key=f"btn_reset_{slot_idx}", on_click=set_slot_custom_ops, args=(slot_idx, slot_avail_ops), use_container_width=True)

                            chosen_ops = st.multiselect(
                                "Competitors in Slot:",
                                options=all_route_ops,
                                default=st.session_state[slot_key],
                                key=f"ms_slot_{slot_idx}",
                                placeholder="Select competitors..."
                            )
                            st.session_state[slot_key] = chosen_ops

                            slot_display_df = slot_df_full[slot_df_full["operator_name"].isin(chosen_ops)].sort_values(
                                by=["departure_time", "effective_fare"], ascending=[True, True]
                            )
                            clean_tbl = build_slot_pricing_table(slot_display_df, is_my_brand_active, my_service_brand)
                            if not clean_tbl.empty:
                                st.dataframe(clean_tbl, use_container_width=True, hide_index=True)
                                csv_slot = clean_tbl.to_csv(index=False).encode('utf-8')
                                st.download_button(
                                    label="📥 CSV",
                                    data=csv_slot,
                                    file_name=f"pricing_matrix_{w_data['label'].replace(':', '').replace(' ', '_')}.csv",
                                    mime="text/csv",
                                    key=f"csv_slot_{slot_idx}",
                                    use_container_width=True
                                )
                            else:
                                st.caption("No matching buses.")

        # Render the 8 cards in 4 columns x 2 rows (strictly uniform and proportional)
        row1_cols = st.columns(4)
        for i in range(4):
            render_slot_card(win_stats[i], row1_cols[i])

        row2_cols = st.columns(4)
        for i in range(4, 8):
            render_slot_card(win_stats[i], row2_cols[i - 4])

        # ---------------------------------------------------------------------
        # DEDICATED FULL-WIDTH SLOT INSPECTOR PANEL (FIT TO SCREEN)
        # ---------------------------------------------------------------------
        raw_insp_slot = st.session_state.get("tab2_active_inspect_slot")
        active_inspect_slot = None
        if raw_insp_slot is not None:
            try:
                active_inspect_slot = int(raw_insp_slot)
            except Exception:
                active_inspect_slot = None

        if sec1_layout_mode == "🖥️ Full-Width Inspector (Fit to Screen)" and active_inspect_slot is not None and 0 <= active_inspect_slot < 8:
            active_w = TIME_WINDOWS_3HR[active_inspect_slot]
            slot_df_full = df_3hr[df_3hr["win_idx"] == active_inspect_slot]
            slot_avail_ops = sorted(slot_df_full["operator_name"].dropna().unique().tolist())
            slot_key = f"slot_custom_ops_{active_inspect_slot}"
            insp_ms_key = f"ms_inspector_slot_{active_inspect_slot}"
            
            # Ensure both session keys exist and are synchronized
            if insp_ms_key in st.session_state:
                current_selected = [op for op in st.session_state[insp_ms_key] if op in all_route_ops]
                st.session_state[slot_key] = current_selected
            elif slot_key in st.session_state:
                current_selected = [op for op in st.session_state[slot_key] if op in all_route_ops]
                st.session_state[insp_ms_key] = current_selected
            else:
                current_selected = slot_avail_ops
                st.session_state[slot_key] = slot_avail_ops
                st.session_state[insp_ms_key] = slot_avail_ops

            slot_filtered = slot_df_full[slot_df_full["operator_name"].isin(current_selected)]
            
            if not slot_filtered.empty:
                slot_med = slot_filtered["effective_fare"].median()
                slot_min = slot_filtered["effective_fare"].min()
                slot_max = slot_filtered["effective_fare"].max()
                slot_buses = len(slot_filtered)
                slot_ops = slot_filtered["operator_name"].nunique()
                slot_seats = int(slot_filtered["seats_available"].sum())
            else:
                slot_med = slot_min = slot_max = None
                slot_buses = slot_ops = slot_seats = 0

            st.write("")
            with st.container(border=True):
                # Header with Title and Close Button
                insp_hdr_col1, insp_hdr_col2 = st.columns([5, 1])
                with insp_hdr_col1:
                    st.markdown(f"### 📋 Slot Competitor Customizer & Enterprise Pricing Matrix — {active_w['icon']} {active_w['label']} ({active_w['period']})")
                    st.caption(f"Isolating **{len(slot_df_full)}** bus departures and **{len(slot_avail_ops)}** competitors in this 3-hour window. Adjust tracked competitors below to recalibrate dynamic pricing benchmarks.")
                with insp_hdr_col2:
                    st.button("✖️ Close Inspector", key="btn_close_full_inspector", on_click=toggle_slot_inspect, args=(active_inspect_slot,), use_container_width=True)

                # 4-KPI Metric Strip for Active Window
                kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
                with kpi_col1:
                    med_str = f"₹{int(slot_med):,}" if slot_med is not None else "—"
                    st.metric("Dynamic Median Fare", med_str, help="Median effective fare of all active competitors in this slot")
                with kpi_col2:
                    min_str = f"₹{int(slot_min):,}" if slot_min is not None else "—"
                    st.metric("Lowest Market Fare", min_str, help="Minimum ticket price currently available in this slot")
                with kpi_col3:
                    max_str = f"₹{int(slot_max):,}" if slot_max is not None else "—"
                    st.metric("Peak Surge Fare", max_str, help="Highest premium fare observed in this window")
                with kpi_col4:
                    st.metric("Capacity & Operators", f"{slot_buses} Buses · {slot_seats} Seats", f"{slot_ops} Competitors")

                st.markdown("---")

                # Multi-Operator Filtering Toolbar (Full Width - No Stacking!)
                st.markdown("##### ➕ Multi-Operator Competitor Filtering")
                filter_c1, filter_c2 = st.columns([3.5, 2.5])
                with filter_c1:
                    def _on_insp_ms_change():
                        st.session_state[slot_key] = st.session_state.get(insp_ms_key, [])

                    selected_ops_insp = st.multiselect(
                        f"Select Competitors to benchmark in {active_w['label']}:",
                        options=all_route_ops,
                        key=insp_ms_key,
                        on_change=_on_insp_ms_change,
                        placeholder="Choose competitors (e.g. FlixBus, NueGo, zingbus plus, LeafyBus...)"
                    )
                    st.session_state[slot_key] = selected_ops_insp
                with filter_c2:
                    st.markdown("<small style='color:#6B7280; font-weight:600;'>⚡ Quick Benchmark Presets:</small>", unsafe_allow_html=True)
                    p_btn1, p_btn2, p_btn3, p_btn4 = st.columns(4)
                    with p_btn1:
                        st.button(
                            "⭐ All in Slot",
                            key=f"insp_btn_all_{active_inspect_slot}",
                            on_click=set_slot_custom_ops,
                            args=(active_inspect_slot, slot_avail_ops),
                            use_container_width=True,
                            help="Select all operators with departures in this time slot"
                        )
                    with p_btn2:
                        top5_in_slot = slot_df_full["operator_name"].value_counts().head(5).index.tolist()
                        if not top5_in_slot:
                            top5_in_slot = all_route_ops[:5]
                        st.button(
                            "🏆 Top 5",
                            key=f"insp_btn_top5_{active_inspect_slot}",
                            on_click=set_slot_custom_ops,
                            args=(active_inspect_slot, top5_in_slot),
                            use_container_width=True,
                            help="Select top 5 competitors by service frequency in this slot"
                        )
                    with p_btn3:
                        rtc_in_slot = slot_df_full[slot_df_full["is_rtc"] == 1]["operator_name"].dropna().unique().tolist()
                        all_rtc_route = sorted(df_3hr[df_3hr["is_rtc"] == 1]["operator_name"].dropna().unique().tolist())
                        rtc_targets = rtc_in_slot if rtc_in_slot else all_rtc_route
                        st.button(
                            "🏛️ RTC Gov",
                            key=f"insp_btn_rtc_{active_inspect_slot}",
                            on_click=set_slot_custom_ops,
                            args=(active_inspect_slot, rtc_targets),
                            use_container_width=True,
                            help="Filter to state transport corporations"
                        )
                    with p_btn4:
                        st.button(
                            "🔄 Reset",
                            key=f"insp_btn_reset_{active_inspect_slot}",
                            on_click=set_slot_custom_ops,
                            args=(active_inspect_slot, slot_avail_ops),
                            use_container_width=True,
                            help="Reset selection to all available operators in this slot"
                        )

                # Active Services DataFrame (Enterprise Pricing Matrix)
                slot_display_df = slot_df_full[slot_df_full["operator_name"].isin(st.session_state[slot_key])].sort_values(
                    by=["departure_time", "effective_fare"], ascending=[True, True]
                )

                st.markdown("##### 📋 Enterprise Pricing Matrix (Live Bus Listings)")
                clean_tbl = build_slot_pricing_table(slot_display_df, is_my_brand_active, my_service_brand)

                if not clean_tbl.empty:
                    st.dataframe(
                        clean_tbl,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Time": st.column_config.TextColumn("Time", width="small"),
                            "Service (Operator Name)": st.column_config.TextColumn("Service (Operator Name)", width="large"),
                            "Service Type (Bus/Coach model)": st.column_config.TextColumn("Service Type (Bus/Coach model)", width="large"),
                            "Fare (₹)": st.column_config.TextColumn("Fare (₹)", width="medium"),
                            "Seats Left": st.column_config.NumberColumn("Seats Left", width="small"),
                        }
                    )

                    # Export & Action Footer
                    dl_c1, dl_c2 = st.columns([1, 3])
                    with dl_c1:
                        csv_slot = clean_tbl.to_csv(index=False).encode('utf-8')
                        st.download_button(
                            label=f"📥 Download {active_w['label']} Matrix (CSV)",
                            data=csv_slot,
                            file_name=f"pricing_matrix_{active_w['label'].replace(':', '').replace(' ', '_')}_{selected_doj}.csv",
                            mime="text/csv",
                            key=f"csv_inspector_slot_{active_inspect_slot}",
                            use_container_width=True
                        )
                    with dl_c2:
                        st.caption(f"Showing **{len(clean_tbl)}** bus services matching **{len(selected_ops_insp)}** selected operators in this window.")
                else:
                    st.info("No competitor bus services match your active operator selection for this time slot. Click '⭐ All in Slot' or select operators above.")

        st.markdown("---")

        # ---------------------------------------------------------------------
        # Section 2: ➕ OPERATOR ADD-TO-COMPARE BENCHMARK FILTER
        # ---------------------------------------------------------------------
        st.markdown("#### 2. ➕ Add Operators to Compare (Competitor Benchmark Filter)")
        st.caption("Select or search specific bus operators to compare head-to-head across all 3-hour windows, dynamic pricing bands, and service listings.")

        all_route_ops = sorted(df_3hr["operator_name"].dropna().unique().tolist())
        op_counts = df_3hr["operator_name"].value_counts()
        top5_list = op_counts.head(5).index.tolist()
        top10_list = op_counts.head(10).index.tolist()
        rtc_list = sorted(df_3hr[df_3hr["is_rtc"] == 1]["operator_name"].dropna().unique().tolist())

        # Initialize session state for operator comparison selection
        if "tab2_compare_ops" not in st.session_state:
            st.session_state["tab2_compare_ops"] = top5_list if top5_list else all_route_ops[:5]

        # Sanitize session state in case route changed
        st.session_state["tab2_compare_ops"] = [
            op for op in st.session_state["tab2_compare_ops"] if op in all_route_ops
        ]
        if not st.session_state["tab2_compare_ops"] and all_route_ops:
            st.session_state["tab2_compare_ops"] = top5_list if top5_list else all_route_ops[:5]

        def set_tab2_compare_ops(new_ops):
            st.session_state["tab2_compare_ops"] = [op for op in new_ops if op in all_route_ops]

        # Quick preset filter buttons
        preset_c1, preset_c2, preset_c3, preset_c4, preset_c5 = st.columns(5)
        with preset_c1:
            st.button("⭐ Top 5 Competitors", on_click=set_tab2_compare_ops, args=(top5_list,), use_container_width=True, help="Compare top 5 operators by service volume")
        with preset_c2:
            st.button("🏆 Top 10 Competitors", on_click=set_tab2_compare_ops, args=(top10_list,), use_container_width=True, help="Compare top 10 operators")
        with preset_c3:
            st.button("🏛️ RTC Gov Only", on_click=set_tab2_compare_ops, args=(rtc_list if rtc_list else top5_list,), use_container_width=True, help="Filter to state transport corporations")
        with preset_c4:
            st.button("✨ Select All Operators", on_click=set_tab2_compare_ops, args=(all_route_ops,), use_container_width=True, help="Select all active operators on this route")
        with preset_c5:
            st.button("❌ Clear Selection", on_click=set_tab2_compare_ops, args=([],), use_container_width=True, help="Clear all selected operators")

        selected_compare_operators = st.multiselect(
            "➕ Add / Select Operators to Compare in Matrix & Analytics:",
            options=all_route_ops,
            key="tab2_compare_ops",
            placeholder="Type or select operators (e.g. NueGo, Maharani Travels, RSRTC, IntrCity SmartBus...)",
            help="All tables, heatmaps, and service lists below will dynamically adjust to compare these selected operators."
        )

        active_compare_ops = selected_compare_operators if selected_compare_operators else all_route_ops

        # 🥊 HEAD-TO-HEAD COMPETITOR SUMMARY CARDS (when 2 or more operators selected)
        if len(selected_compare_operators) >= 2:
            st.markdown("##### 🥊 Head-to-Head Competitor Comparison Overview")
            ops_to_display = selected_compare_operators[:8]  # Up to 8 cards
            cols_per_row = min(4, len(ops_to_display))

            for row_start in range(0, len(ops_to_display), cols_per_row):
                chunk = ops_to_display[row_start:row_start + cols_per_row]
                card_cols = st.columns(cols_per_row)
                for idx, op_name in enumerate(chunk):
                    with card_cols[idx]:
                        op_sub = df_3hr[df_3hr["operator_name"] == op_name]
                        if not op_sub.empty:
                            med_p = op_sub["effective_fare"].median()
                            min_p = op_sub["effective_fare"].min()
                            max_p = op_sub["effective_fare"].max()
                            b_cnt = len(op_sub)
                            seats_cnt = int(op_sub["seats_available"].sum())
                            rating_val = op_sub["rating"].mean()
                            rating_str = f"{rating_val:.1f} ★" if pd.notnull(rating_val) else "N/A"
                            is_rtc_val = (op_sub["is_rtc"].iloc[0] == 1)
                            is_primo_val = (op_sub["is_primo"].iloc[0] == 1)

                            cat_badge = "🏛️ RTC Gov" if is_rtc_val else "🏢 Private"
                            primo_badge = " • ⭐ Primo" if is_primo_val else ""

                            with st.container(border=True):
                                st.markdown(f"**{op_name}**")
                                st.caption(f"{cat_badge}{primo_badge}")
                                st.metric("Median Dynamic Fare", f"₹{int(med_p):,}")
                                st.caption(f"Range: **₹{int(min_p):,} - ₹{int(max_p):,}**\n\n🚍 **{b_cnt}** buses • 🪑 **{seats_cnt}** seats • {rating_str}")
        elif len(selected_compare_operators) == 0:
            st.info("💡 No operator selected. Please pick operators from the multi-select above or click **'⭐ Top 5 Competitors'** / **'✨ Select All Operators'**.")

        st.markdown("---")

        # ---------------------------------------------------------------------
        # Section 3: 24×7 HEATMAP & TRAJECTORY CHARTS (PLOTLY)
        # ---------------------------------------------------------------------
        col_c1, col_c2 = st.columns([1, 1])

        with col_c1:
            st.markdown("#### 3. Competitor Pricing Heatmap (Operator vs. 3-Hr Windows)")
            st.caption("Median dynamic ticket price per competitor across each 3-hour departure block. Blank cells indicate service gaps.")

            if len(selected_compare_operators) >= 2:
                heat_ops = selected_compare_operators
            elif len(selected_compare_operators) == 1:
                other_top = [op for op in top10_list if op != selected_compare_operators[0]]
                heat_ops = selected_compare_operators + other_top[:9]
            else:
                heat_ops = df_3hr["operator_name"].value_counts().head(16).index.tolist()

            heat_subset = df_3hr[df_3hr["operator_name"].isin(heat_ops)].copy()

            heat_pivot = heat_subset.pivot_table(
                index="operator_name",
                columns="win_label",
                values="effective_fare",
                aggfunc="median"
            )
            window_order = [w["label"] for w in TIME_WINDOWS_3HR]
            heat_pivot = heat_pivot.reindex(columns=window_order)

            hover_text = []
            for op in heat_pivot.index:
                row_hover = []
                for win_lbl in heat_pivot.columns:
                    match_buses = heat_subset[(heat_subset["operator_name"] == op) & (heat_subset["win_label"] == win_lbl)]
                    if not match_buses.empty:
                        med = match_buses["effective_fare"].median()
                        low = match_buses["effective_fare"].min()
                        high = match_buses["effective_fare"].max()
                        cnt = len(match_buses)
                        row_hover.append(f"<b>{op}</b><br>Window: {win_lbl}<br>Median Fare: ₹{int(med):,}<br>Fare Range: ₹{int(low)} - ₹{int(high)}<br>Buses: {cnt}")
                    else:
                        row_hover.append(f"<b>{op}</b><br>Window: {win_lbl}<br>No Services")
                hover_text.append(row_hover)

            fig_heat = go.Figure(data=go.Heatmap(
                z=heat_pivot.values,
                x=heat_pivot.columns,
                y=heat_pivot.index,
                text=hover_text,
                hoverinfo="text",
                colorscale="YlOrRd",
                colorbar=dict(title="Median Fare (₹)"),
                hoverongaps=False
            ))
            fig_heat.update_layout(
                xaxis_title="3-Hour Departure Window",
                yaxis_title="Competitor Operator",
                height=480,
                margin=dict(l=20, r=20, t=30, b=50)
            )
            st.plotly_chart(fig_heat, use_container_width=True)

        with col_c2:
            st.markdown("#### 4. 24-Hour Price Curve & Departure Frequency")
            st.caption("Market-wide dynamic fare spread (min, median, peak surge) against bus departure capacity.")

            fig_curve = go.Figure()
            clean_win = win_stats_df.dropna(subset=["median_fare"])

            if not clean_win.empty:
                fig_curve.add_trace(go.Bar(
                    x=clean_win["label"],
                    y=clean_win["buses"],
                    name="Bus Departures",
                    yaxis="y2",
                    marker_color="rgba(156, 163, 175, 0.25)"
                ))

                fig_curve.add_trace(go.Scatter(
                    x=clean_win["label"],
                    y=clean_win["max_fare"],
                    name="Peak Surge Fare",
                    mode="lines",
                    line=dict(color="rgba(239, 68, 68, 0.4)", dash="dot", width=1.5)
                ))

                fig_curve.add_trace(go.Scatter(
                    x=clean_win["label"],
                    y=clean_win["median_fare"],
                    name="Median Dynamic Fare",
                    mode="lines+markers",
                    line=dict(color="#D84E55", width=3.5),
                    marker=dict(size=8, color="#D84E55")
                ))

                fig_curve.add_trace(go.Scatter(
                    x=clean_win["label"],
                    y=clean_win["min_fare"],
                    name="Lowest Available Fare",
                    mode="lines",
                    line=dict(color="rgba(16, 185, 129, 0.6)", dash="dot", width=1.5)
                ))

                fig_curve.update_layout(
                    xaxis_title="3-Hour Departure Window",
                    yaxis=dict(title="Effective Fare (₹)", side="left"),
                    yaxis2=dict(title="Total Bus Services", overlaying="y", side="right", showgrid=False),
                    height=480,
                    legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.1)"),
                    margin=dict(l=20, r=20, t=30, b=50)
                )
                st.plotly_chart(fig_curve, use_container_width=True)
            else:
                st.info("No window data available for chart.")

        st.markdown("---")

        # ---------------------------------------------------------------------
        # Section 4: 24×7 OPERATOR PRICE BENCHMARK PIVOT MATRIX
        # ---------------------------------------------------------------------
        st.markdown("#### 5. 24×7 Operator Price Benchmark Matrix (Every 3 Hours)")
        st.caption("Comprehensive pivot table displaying each operator's median dynamic fare across all eight 3-hour windows.")

        # Filter source data to active_compare_ops
        matrix_source_df = df_3hr[df_3hr["operator_name"].isin(active_compare_ops)].copy()

        filter_tbl_col1, filter_tbl_col2 = st.columns([3, 1])
        with filter_tbl_col1:
            matrix_search = st.text_input("🔍 Quick Filter Operator Name in Matrix:", placeholder="e.g. NueGo, Laxmi, RSRTC, Zingbus...", key="matrix_text_search")

        if matrix_source_df.empty:
            st.info("No bus services match the active operator filter. Please choose operators from the filter above.")
        else:
            pivot_ops = matrix_source_df.groupby(["operator_id", "operator_name", "is_rtc", "is_primo", "win_label"]).agg(
                med_fare=("effective_fare", "median"),
                min_fare=("effective_fare", "min"),
                bus_cnt=("service_id", "count")
            ).reset_index()

            pivot_ops["cell_display"] = pivot_ops.apply(
                lambda r: f"₹{int(r['med_fare']):,} ({r['bus_cnt']})" if r['bus_cnt'] > 1 else f"₹{int(r['med_fare']):,}",
                axis=1
            )

            matrix_df = pivot_ops.pivot_table(
                index=["operator_name", "is_rtc", "is_primo"],
                columns="win_label",
                values="cell_display",
                aggfunc="first"
            ).fillna("—").reset_index()

            available_cols = [w["label"] for w in TIME_WINDOWS_3HR if w["label"] in matrix_df.columns]

            # Calculate overall metrics per operator
            op_summary = matrix_source_df.groupby("operator_name").agg(
                total_buses=("service_id", "count"),
                overall_med=("effective_fare", "median"),
                min_fare=("effective_fare", "min"),
                max_fare=("effective_fare", "max"),
                avg_rating=("rating", "mean")
            ).reset_index()

            op_summary["Overall Median"] = op_summary["overall_med"].apply(lambda x: f"₹{int(x):,}" if pd.notnull(x) else "—")
            op_summary["Fare Range"] = op_summary.apply(lambda r: f"₹{int(r['min_fare']):,} - ₹{int(r['max_fare']):,}" if pd.notnull(r['min_fare']) else "—", axis=1)
            op_summary["Avg Rating"] = op_summary["avg_rating"].apply(lambda x: f"{x:.1f} ★" if pd.notnull(x) else "—")

            merged_matrix = pd.merge(
                matrix_df,
                op_summary[["operator_name", "total_buses", "Overall Median", "Fare Range", "Avg Rating"]],
                on="operator_name",
                how="left"
            )

            merged_matrix["Category"] = merged_matrix["is_rtc"].apply(lambda x: "RTC Gov" if x == 1 else "Private")
            merged_matrix["Primo"] = merged_matrix["is_primo"].apply(lambda x: "⭐ Yes" if x == 1 else "No")

            final_matrix = merged_matrix[["operator_name", "Category", "Primo", "total_buses", "Overall Median", "Fare Range", "Avg Rating"] + available_cols].rename(columns={
                "operator_name": "Operator Name",
                "total_buses": "Buses"
            })

            if matrix_search:
                q_m = matrix_search.lower()
                final_matrix = final_matrix[final_matrix["Operator Name"].str.lower().str.contains(q_m, na=False)]

            with filter_tbl_col2:
                csv_matrix = final_matrix.to_csv(index=False).encode('utf-8')
                st.download_button(
                    "📥 Export Matrix CSV",
                    data=csv_matrix,
                    file_name=f"3hr_benchmark_matrix_{selected_origin_name}_{selected_dest_name}_{selected_doj}.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            st.dataframe(
                final_matrix,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Operator Name": st.column_config.TextColumn("Operator", width="medium"),
                    "Category": st.column_config.TextColumn("Category", width="small"),
                    "Primo": st.column_config.TextColumn("Primo", width="small"),
                    "Buses": st.column_config.NumberColumn("Buses", width="small"),
                    "Overall Median": st.column_config.TextColumn("Median Fare", width="small"),
                    "Fare Range": st.column_config.TextColumn("Price Spread", width="medium"),
                    "Avg Rating": st.column_config.TextColumn("Rating", width="small"),
                }
            )

        st.markdown("---")

        # ---------------------------------------------------------------------
        # Section 5: SERVICE-BY-SERVICE DEEP DIVE (3-HOUR WINDOW SELECTOR)
        # ---------------------------------------------------------------------
        st.markdown("#### 6. Service-by-Service Deep Dive (Compare Every Service Head-to-Head)")
        st.caption("Isolate any 3-hour departure window to compare individual bus services head-to-head on pricing, coach class, discounts, and seat inventory.")

        filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])

        window_options = ["All 24 Hours (Full Schedule)"] + [f"{w['icon']} {w['label']} ({w['period']})" for w in TIME_WINDOWS_3HR]

        with filter_col1:
            chosen_window = st.selectbox("Select 3-Hour Time Window:", options=window_options, index=0)

        with filter_col2:
            sort_choice = st.selectbox(
                "Sort Services By:",
                options=[
                    "Dynamic Fare (Lowest First)",
                    "Departure Time (Chronological)",
                    "Highest Rating",
                    "Most Seats Available",
                    "Steepest Discount %"
                ]
            )

        with filter_col3:
            service_search = st.text_input("🔍 Filter Bus / Operator in Window:", placeholder="e.g. Sleeper, AC, NueGo...")

        # Option to filter service listings by comparison operators
        if selected_compare_operators and len(selected_compare_operators) < len(all_route_ops):
            filter_compare_only = st.checkbox(
                f"🎯 Filter services to only the {len(selected_compare_operators)} selected comparison operators",
                value=True
            )
        else:
            filter_compare_only = False

        filtered_services = df_3hr.copy()
        if filter_compare_only and selected_compare_operators:
            filtered_services = filtered_services[filtered_services["operator_name"].isin(selected_compare_operators)]

        if chosen_window != "All 24 Hours (Full Schedule)":
            for w in TIME_WINDOWS_3HR:
                if w["label"] in chosen_window:
                    filtered_services = filtered_services[filtered_services["win_idx"] == w["index"]]
                    break

        if service_search:
            q_s = service_search.lower()
            filtered_services = filtered_services[
                filtered_services["operator_name"].str.lower().str.contains(q_s, na=False) |
                filtered_services["bus_type"].str.lower().str.contains(q_s, na=False) |
                filtered_services["service_id"].astype(str).str.contains(q_s, na=False)
            ]

        if sort_choice == "Dynamic Fare (Lowest First)":
            filtered_services = filtered_services.sort_values(by=["effective_fare", "departure_time"], ascending=[True, True])
        elif sort_choice == "Departure Time (Chronological)":
            filtered_services = filtered_services.sort_values(by="departure_time", ascending=True)
        elif sort_choice == "Highest Rating":
            filtered_services = filtered_services.sort_values(by=["rating", "effective_fare"], ascending=[False, True])
        elif sort_choice == "Most Seats Available":
            filtered_services = filtered_services.sort_values(by=["seats_available", "effective_fare"], ascending=[False, True])
        elif sort_choice == "Steepest Discount %":
            filtered_services = filtered_services.sort_values(by=["discount_pct", "effective_fare"], ascending=[False, True])

        st.markdown(f"**Matching Bus Services:** `{len(filtered_services)}` listings")

        serv_display = filtered_services.copy()
        serv_display["Category"] = serv_display["is_rtc"].apply(lambda x: "RTC Gov" if x == 1 else "Private")
        serv_display["Primo"] = serv_display["is_primo"].apply(lambda x: "⭐ Primo" if x == 1 else "Standard")
        serv_display["Effective Fare"] = serv_display["effective_fare"].apply(lambda x: f"₹{int(x):,}")
        serv_display["Base Fare"] = serv_display["original_fare"].apply(lambda x: f"₹{int(x):,}" if pd.notnull(x) and x > 0 else "—")
        serv_display["Discount %"] = serv_display["discount_pct"].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) and x > 0 else "0.0%")
        serv_display["Rating (★)"] = serv_display["rating"].apply(lambda x: f"{x:.1f} ★" if pd.notnull(x) else "N/A")
        serv_display["Reviews"] = serv_display["review_count"].apply(lambda x: f"{int(x):,}" if pd.notnull(x) and x > 0 else "—")

        def extract_first_loc(json_str):
            if not json_str:
                return "—"
            try:
                data = json.loads(json_str)
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    return data[0].get("location", "—")
            except Exception:
                pass
            return "—"

        serv_display["Pickup"] = serv_display["boarding_points_json"].apply(extract_first_loc)
        serv_display["Dropoff"] = serv_display["dropping_points_json"].apply(extract_first_loc)

        table_3hr_cols = [
            "service_id", "departure_time", "win_label", "operator_name", "Category",
            "Primo", "bus_type", "Effective Fare", "Base Fare", "Discount %",
            "seats_available", "Rating (★)", "Reviews", "Pickup", "Dropoff"
        ]

        final_serv_table = serv_display[table_3hr_cols].rename(columns={
            "service_id": "Service ID",
            "departure_time": "Dep Time",
            "win_label": "3-Hr Window",
            "operator_name": "Operator Name",
            "bus_type": "Bus Coach Type",
            "seats_available": "Seats Left",
        })

        st.dataframe(
            final_serv_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Operator Name": st.column_config.TextColumn("Operator", width="medium"),
                "Bus Coach Type": st.column_config.TextColumn("Coach Class", width="medium"),
                "Effective Fare": st.column_config.TextColumn("Dynamic Fare", width="small"),
                "Seats Left": st.column_config.NumberColumn("Seats", width="small"),
            }
        )

        csv_data = final_serv_table.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="⬇️ Download Filtered 3-Hour Service Comparison (CSV)",
            data=csv_data,
            file_name=f"3hour_price_comparison_{selected_origin_name}_{selected_dest_name}_{selected_doj}.csv",
            mime="text/csv"
        )

        st.markdown("---")

        # ---------------------------------------------------------------------
        # Section 7: MULTI-SNAPSHOT TIME-SERIES VOLATILITY TRACKER
        # ---------------------------------------------------------------------
        st.markdown("#### 7. Multi-Snapshot Price Volatility Tracker (Surge & Discount Alerts)")
        st.caption("Tracks how ticket prices for the same bus service change dynamically across recurring scrape runs.")

        with get_db_connection(DB_PATH) as conn:
            vol_query = """
                SELECT 
                    service_id,
                    operator_name,
                    departure_time,
                    bus_type,
                    COUNT(DISTINCT scraped_at) as snapshot_count,
                    MIN(effective_fare) as min_fare,
                    MAX(effective_fare) as max_fare,
                    ROUND(MAX(effective_fare) - MIN(effective_fare), 2) as fare_diff,
                    ROUND((MAX(effective_fare) - MIN(effective_fare)) / MIN(effective_fare) * 100, 1) as surge_pct
                FROM fare_snapshots
                WHERE from_city_id = ? AND to_city_id = ? AND travel_date = ?
                GROUP BY service_id, operator_name
                HAVING snapshot_count > 1
                ORDER BY fare_diff DESC
                LIMIT 20;
            """
            vol_df = pd.read_sql_query(vol_query, conn, params=[selected_origin_id, selected_dest_id, selected_doj])

        if not vol_df.empty:
            vol_display = vol_df.copy()
            vol_display["Status"] = vol_display["fare_diff"].apply(
                lambda x: "🔺 Dynamic Surge" if x > 0 else "➖ Stable Pricing"
            )
            vol_display["Min Fare"] = vol_display["min_fare"].apply(lambda x: f"₹{int(x):,}")
            vol_display["Max Fare"] = vol_display["max_fare"].apply(lambda x: f"₹{int(x):,}")
            vol_display["Fare Delta (₹)"] = vol_display["fare_diff"].apply(lambda x: f"+₹{int(x):,}" if x > 0 else "₹0")
            vol_display["Surge %"] = vol_display["surge_pct"].apply(lambda x: f"+{x:.1f}%" if x > 0 else "0.0%")

            st.dataframe(
                vol_display[[
                    "service_id", "operator_name", "departure_time", "bus_type",
                    "snapshot_count", "Min Fare", "Max Fare", "Fare Delta (₹)", "Surge %", "Status"
                ]].rename(columns={
                    "service_id": "Service ID",
                    "operator_name": "Operator Name",
                    "departure_time": "Dep Time",
                    "bus_type": "Coach Type",
                    "snapshot_count": "Snapshots Observed"
                }),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("💡 To view longitudinal price changes over time, run the scraper again at different hours using the **'⚡ Refresh & Fetch Live Data'** button above.")


# =============================================================================
# TAB 3: 📊 COMPETITOR DYNAMIC PRICING ANALYTICS
# =============================================================================
with tab_analytics:
    st.subheader(f"📊 Market Dynamic Pricing Visual Analytics ({selected_origin_name} $\\rightarrow$ {selected_dest_name})")

    if snapshots_df.empty:
        st.info("No data available to plot. Select an active route or click 'Refresh & Fetch Live Data'.")
    else:
        st.markdown("#### 1. Dynamic Fare Distribution Across Competitors")
        st.caption("Side-by-side distribution showing lowest, median, and peak surge fares per operator.")

        top_ops = snapshots_df["operator_name"].value_counts().head(15).index.tolist()
        chart_df = snapshots_df[snapshots_df["operator_name"].isin(top_ops)].copy()

        fig_box = px.box(
            chart_df,
            x="operator_name",
            y="effective_fare",
            color="operator_name",
            points="outliers",
            labels={"operator_name": "Competitor", "effective_fare": "Effective Dynamic Fare (₹)"},
            title="Effective Dynamic Fare Range (Min, Median, Max) by Operator",
        )
        fig_box.update_layout(
            showlegend=False,
            xaxis_tickangle=-45,
            height=450,
            margin=dict(l=20, r=20, t=40, b=100)
        )
        st.plotly_chart(fig_box, use_container_width=True)

        col_a1, col_a2 = st.columns(2)

        # Chart 2: Seat Inventory vs. Dynamic Fare
        with col_a1:
            st.markdown("#### 2. Seat Inventory vs. Dynamic Fare (Yield Management)")
            st.caption("Testing whether remaining seat scarcity correlates with price increases.")

            fig_scatter = px.scatter(
                snapshots_df,
                x="seats_available",
                y="effective_fare",
                color="operator_name",
                size="total_seats",
                hover_data=["bus_type", "departure_time", "discount_pct", "rating"],
                labels={"seats_available": "Seats Remaining", "effective_fare": "Effective Fare (₹)"},
                title="Yield Curve: Remaining Inventory vs. Effective Ticket Fare"
            )
            fig_scatter.update_layout(height=420, showlegend=False)
            st.plotly_chart(fig_scatter, use_container_width=True)

        # Chart 3: Departure Hour vs. Median Dynamic Fare
        with col_a2:
            st.markdown("#### 3. Fare Fluctuation by Departure Time of Day")
            st.caption("Analyzing morning vs evening vs night pricing trends.")

            def get_dep_hour(t_str):
                try:
                    if pd.notnull(t_str) and ":" in str(t_str):
                        return int(str(t_str).split(":")[0])
                except Exception:
                    pass
                return None

            hourly_df = snapshots_df.copy()
            hourly_df["dep_hour"] = hourly_df["departure_time"].apply(get_dep_hour)
            hourly_agg = hourly_df.dropna(subset=["dep_hour"]).groupby("dep_hour")["effective_fare"].agg(["median", "min", "count"]).reset_index()

            if not hourly_agg.empty:
                fig_hour = go.Figure()
                fig_hour.add_trace(go.Bar(
                    x=hourly_agg["dep_hour"],
                    y=hourly_agg["count"],
                    name="Bus Count",
                    yaxis="y2",
                    marker_color="rgba(156, 163, 175, 0.3)"
                ))
                fig_hour.add_trace(go.Scatter(
                    x=hourly_agg["dep_hour"],
                    y=hourly_agg["median"],
                    name="Median Fare (₹)",
                    mode="lines+markers",
                    line=dict(color="#D84E55", width=3)
                ))
                fig_hour.update_layout(
                    title="Departure Hour vs Median Dynamic Fare",
                    xaxis=dict(title="Hour of Day (24h format)", tickmode="linear", dtick=2),
                    yaxis=dict(title="Median Fare (₹)", side="left"),
                    yaxis2=dict(title="Bus Frequency", overlaying="y", side="right", showgrid=False),
                    height=420,
                    legend=dict(x=0.01, y=0.99)
                )
                st.plotly_chart(fig_hour, use_container_width=True)
            else:
                st.info("Insufficient departure timestamp data for hourly trend curve.")

        col_b1, col_b2 = st.columns(2)

        # Chart 4: Coach Class Pricing Matrix
        with col_b1:
            st.markdown("#### 4. Comfort Tier Segmentation (AC / Sleeper)")
            st.caption("Fare spread across comfort tiers.")

            def classify_coach(row):
                if row["is_ac"] == 1 and row["is_sleeper"] == 1:
                    return "AC Sleeper"
                elif row["is_ac"] == 1:
                    return "AC Seater"
                elif row["is_sleeper"] == 1:
                    return "Non-AC Sleeper"
                return "Non-AC Seater"

            class_df = snapshots_df.copy()
            class_df["Class"] = class_df.apply(classify_coach, axis=1)
            class_agg = class_df.groupby("Class")["effective_fare"].agg(["count", "min", "median", "max"]).reset_index()

            fig_class = px.bar(
                class_agg,
                x="Class",
                y="median",
                color="Class",
                text_auto="₹%{y:.0f}",
                title="Median Ticket Price by Coach Class Tier",
                labels={"median": "Median Fare (₹)", "Class": "Bus Category"}
            )
            fig_class.update_layout(height=380, showlegend=False)
            st.plotly_chart(fig_class, use_container_width=True)

        # Chart 5: Discount Aggressiveness Leaderboard
        with col_b2:
            st.markdown("#### 5. Competitor Discount & Promo Aggressiveness")
            st.caption("Top operators cutting prices with instant promotional discounts.")

            disc_ops = snapshots_df.groupby("operator_name").agg(
                avg_disc=("discount_pct", "mean"),
                buses=("service_id", "count")
            ).reset_index()
            disc_ops = disc_ops[disc_ops["buses"] >= 2].sort_values(by="avg_disc", ascending=False).head(10)

            if not disc_ops.empty:
                fig_disc = px.bar(
                    disc_ops,
                    x="avg_disc",
                    y="operator_name",
                    orientation="h",
                    color="avg_disc",
                    color_continuous_scale="Reds",
                    text_auto="%{x:.1f}%",
                    title="Top Competitors Offering Steepest Average Discounts",
                    labels={"avg_disc": "Average Discount %", "operator_name": "Operator"}
                )
                fig_disc.update_layout(height=380, yaxis=dict(autorange="reversed"))
                st.plotly_chart(fig_disc, use_container_width=True)
            else:
                st.info("No discount campaigns active for this route.")


# =============================================================================
# TAB 3: 🏙️ MASTER DIRECTORY (CITIES & OPERATORS)
# =============================================================================
with tab_directory:
    st.subheader("🏙️ Master Entity Directory Management")
    st.caption("Manage official redBus numeric City IDs and bus operator records.")

    subtab_cities, subtab_operators = st.tabs(["📍 City Directory", "🏢 Operator Directory"])

    # Subtab A: City Directory
    with subtab_cities:
        all_cities = load_cities(DB_PATH)
        
        c_kpi1, c_kpi2 = st.columns(2)
        with c_kpi1:
            st.metric("Total Registered Cities", len(all_cities))
        with c_kpi2:
            states_count = all_cities["state"].nunique() if not all_cities.empty else 0
            st.metric("States / Territories Covered", states_count)

        city_search = st.text_input("🔍 Search Cities (by name, ID, or state):", placeholder="e.g. Delhi, Jaipur, Agra, 733...")
        filtered_cities = all_cities.copy()
        if city_search:
            q = city_search.lower()
            filtered_cities = filtered_cities[
                filtered_cities["city_name"].str.lower().str.contains(q, na=False) |
                filtered_cities["city_id"].astype(str).str.contains(q, na=False) |
                filtered_cities["state"].str.lower().str.contains(q, na=False)
            ]

        st.dataframe(
            filtered_cities,
            use_container_width=True,
            hide_index=True,
            column_config={
                "city_id": st.column_config.NumberColumn("City ID (redBus)", format="%d"),
                "city_name": "City Name",
                "state": "State / Region",
                "country": "Country"
            }
        )

        with st.expander("➕ Add or Update City Record"):
            with st.form("city_upsert_form", clear_on_submit=True):
                col_c1, col_c2 = st.columns(2)
                with col_c1:
                    new_city_id = st.number_input("redBus City ID*", min_value=1, step=1, value=1290)
                    new_city_name = st.text_input("City Name*", placeholder="e.g. Agra")
                with col_c2:
                    new_state = st.text_input("State / Region", placeholder="e.g. Uttar Pradesh")
                    new_country = st.text_input("Country", value="India")
                
                submitted_city = st.form_submit_button("Save City to Database", use_container_width=True)
                if submitted_city:
                    if not new_city_name.strip():
                        st.error("City Name is required!")
                    else:
                        upsert_city(
                            city_id=int(new_city_id),
                            city_name=new_city_name.strip(),
                            state=new_state.strip() or None,
                            country=new_country.strip() or "India",
                            db_path=DB_PATH
                        )
                        clear_app_cache()
                        st.success(f"Successfully upserted city: {new_city_name} (ID: {new_city_id})")
                        st.rerun()

    # Subtab B: Operator Directory
    with subtab_operators:
        all_ops = load_operators(DB_PATH)

        o_kpi1, o_kpi2, o_kpi3, o_kpi4, o_kpi5 = st.columns(5)
        total_ops_count = len(all_ops)
        rtc_count = len(all_ops[all_ops["is_rtc"] == 1])
        primo_count = len(all_ops[all_ops["is_primo"] == 1])
        private_count = total_ops_count - rtc_count
        total_market_reviews = all_ops["rating_count"].sum() if not all_ops.empty else 0

        with o_kpi1:
            st.metric("Total Operators", total_ops_count)
        with o_kpi2:
            st.metric("Private Operators", private_count)
        with o_kpi3:
            st.metric("RTC Gov Bodies", rtc_count)
        with o_kpi4:
            st.metric("Primo Verified", primo_count)
        with o_kpi5:
            st.metric("Total Reviews Tracked", f"{int(total_market_reviews):,}")

        col_of1, col_of2 = st.columns(2)
        with col_of1:
            op_search = st.text_input("🔍 Search Operators:", placeholder="e.g. NueGo, RSRTC, Laxmi, 31023...")
        with col_of2:
            op_cat_filter = st.selectbox("Category Filter:", ["All Operators", "Private Only", "RTC Government Only", "Primo Verified Only"])

        filtered_ops = all_ops.copy()
        if op_search:
            q_op = op_search.lower()
            filtered_ops = filtered_ops[
                filtered_ops["operator_name"].str.lower().str.contains(q_op, na=False) |
                filtered_ops["operator_id"].astype(str).str.contains(q_op, na=False)
            ]
        if op_cat_filter == "Private Only":
            filtered_ops = filtered_ops[filtered_ops["is_rtc"] == 0]
        elif op_cat_filter == "RTC Government Only":
            filtered_ops = filtered_ops[filtered_ops["is_rtc"] == 1]
        elif op_cat_filter == "Primo Verified Only":
            filtered_ops = filtered_ops[filtered_ops["is_primo"] == 1]

        display_ops = filtered_ops.copy()
        display_ops["Category"] = display_ops["is_rtc"].apply(lambda x: "RTC Gov" if x == 1 else "Private")
        display_ops["Primo"] = display_ops["is_primo"].apply(lambda x: "⭐ Yes" if x == 1 else "No")
        display_ops["Max Rating"] = display_ops["max_rating"].apply(lambda x: f"{x:.1f} ★" if pd.notnull(x) and x > 0 else "N/A")
        display_ops["Rating Count"] = display_ops["rating_count"].apply(lambda x: f"{int(x):,}" if pd.notnull(x) and x > 0 else "-")

        st.dataframe(
            display_ops[[
                "operator_id", "operator_name", "Category", "Primo",
                "Max Rating", "Rating Count", "updated_at"
            ]].rename(columns={
                "operator_id": "Operator ID",
                "operator_name": "Operator Name",
                "updated_at": "Last Synced (UTC)"
            }),
            use_container_width=True,
            hide_index=True
        )

        with st.expander("➕ Add or Update Operator Record"):
            with st.form("operator_upsert_form", clear_on_submit=True):
                col_op1, col_op2 = st.columns(2)
                with col_op1:
                    new_op_id = st.number_input("Operator ID*", min_value=1, step=1, value=31023)
                    new_op_name = st.text_input("Operator Name*", placeholder="e.g. LeafyBus Express")
                    new_is_rtc = st.checkbox("Is State RTC Government Transport?")
                with col_op2:
                    new_is_primo = st.checkbox("Is redBus Primo Verified?")
                    new_max_rating = st.number_input("Customer Rating (0.0 - 5.0)", min_value=0.0, max_value=5.0, value=4.5, step=0.1)
                    new_review_count = st.number_input("Total Rating / Review Count", min_value=0, value=500, step=10)

                submitted_op = st.form_submit_button("Save Operator to Database", use_container_width=True)
                if submitted_op:
                    if not new_op_name.strip():
                        st.error("Operator Name is required!")
                    else:
                        upsert_operator(
                            operator_id=int(new_op_id),
                            operator_name=new_op_name.strip(),
                            is_rtc=1 if new_is_rtc else 0,
                            is_primo=1 if new_is_primo else 0,
                            max_rating=float(new_max_rating),
                            rating_count=int(new_review_count),
                            db_path=DB_PATH
                        )
                        clear_app_cache()
                        st.success(f"Successfully upserted operator: {new_op_name} (ID: {new_op_id})")
                        st.rerun()


# =============================================================================
# TAB 4: 📥 EXCEL EXPORT CENTER
# =============================================================================
with tab_export:
    st.subheader("📥 Professional Excel Market Intelligence Export")
    st.caption("Generate a formatted multi-sheet Excel spreadsheet (`.xlsx`) containing executive summary metrics, operator benchmarking, and all bus listings.")

    exp_col1, exp_col2 = st.columns([1, 1])
    with exp_col1:
        st.markdown(f"""
        **Active Route & Criteria for Export:**
        - **Origin City:** {selected_origin_name} (ID: `{selected_origin_id}`)
        - **Destination City:** {selected_dest_name} (ID: `{selected_dest_id}`)
        - **Journey Date (DOJ):** `{selected_doj}`
        - **Total Matching Bus Listings:** `{len(snapshots_df)}`
        - **Distinct Competitors Included:** `{snapshots_df['operator_id'].nunique() if not snapshots_df.empty else 0}`
        """)

    with exp_col2:
        st.markdown("""
        **Included Excel Tabs:**
        1. **`Executive Summary`**: High-level market metrics, min/median/max dynamic fares, and capacity.
        2. **`Operator Benchmarking`**: Aggregated competitor breakdown (buses, dynamic fare spread, seat share, ratings).
        3. **`All Bus Services`**: Granular bus-by-bus service listings with dynamic fares, discount %, and coach classes.
        """)

    if snapshots_df.empty:
        st.warning("No bus records match the current route and filters. Cannot generate export.")
    else:
        excel_bytes = generate_excel_report(
            snapshots_df=snapshots_df,
            from_name=selected_origin_name,
            to_name=selected_dest_name,
            travel_date=selected_doj
        )

        clean_from = selected_origin_name.lower().replace(" ", "_")
        clean_to = selected_dest_name.lower().replace(" ", "_")
        clean_date = selected_doj.replace("-", "_")
        export_filename = f"bus_market_analysis_{clean_from}_to_{clean_to}_{clean_date}.xlsx"

        st.download_button(
            label=f"⬇️ Download Professional Excel Report ({export_filename})",
            data=excel_bytes,
            file_name=export_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

        st.markdown("---")
        st.markdown("#### 👁️ Excel Data Preview (First 10 Listings)")
        preview_cols = ["operator_name", "bus_type", "departure_time", "arrival_time", "effective_fare", "discount_pct", "seats_available", "rating"]
        st.dataframe(snapshots_df[preview_cols].head(10), use_container_width=True)
