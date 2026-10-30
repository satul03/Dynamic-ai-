"""
alerts.py - Competitor Price Alerting & Multi-Channel Webhook Notification Engine

Monitors real-time and historical fare_snapshots to detect:
1. Significant competitor price cuts (>= 10% drop vs. previous snapshot).
2. Last-minute dynamic fare surges inside 24 hours of departure.
3. Imminent competitor inventory exhaustion (<= 3 seats remaining inside 12 hours).

Dispatches structured alerts via:
- Telegram Bot API
- Slack Incoming Webhook
- SQLite Persistence (price_alerts table)
- High-visibility Terminal / Logging fallback
"""

import os
import sys
import json
import logging
import argparse
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import urllib.request
import urllib.parse
import pandas as pd

from db import get_db_connection, insert_price_alerts, DEFAULT_DB_PATH

# Configure stdout and logging
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logger = logging.getLogger("PriceAlertsEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class PriceAlert:
    alert_id: str
    created_at: str
    alert_type: str              # PRICE_DROP, LAST_MINUTE_SURGE, LOW_INVENTORY
    severity: str                # INFO, WARNING, CRITICAL
    operator_id: int
    operator_name: str
    service_id: str
    from_city_id: int
    to_city_id: int
    travel_date: str
    departure_time: str
    hours_to_departure: float
    old_fare: float
    new_fare: float
    fare_delta: float
    fare_delta_pct: float
    seats_remaining: int
    message: str
    dispatched_channels: str


class AlertDispatcher:
    """Manages dispatching alerts to Slack, Telegram, and SQLite."""

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None
    ):
        self.slack_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self.tg_token = telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")

    def send_slack(self, alert: PriceAlert) -> bool:
        """Sends rich formatted Slack Block Kit message via incoming webhook."""
        if not self.slack_url:
            return False

        color = "#DC2626" if alert.severity == "CRITICAL" else "#F59E0B" if alert.severity == "WARNING" else "#2563EB"
        
        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"🚨 {alert.alert_type}: {alert.operator_name}",
                    "text": alert.message,
                    "fields": [
                        {"title": "Operator", "value": alert.operator_name, "short": True},
                        {"title": "Route & Date", "value": f"City {alert.from_city_id} ➔ {alert.to_city_id} ({alert.travel_date})", "short": True},
                        {"title": "Price Shift", "value": f"₹{int(alert.old_fare)} ➔ ₹{int(alert.new_fare)} ({alert.fare_delta_pct:+.1f}%)", "short": True},
                        {"title": "Seats & Hours", "value": f"💺 {alert.seats_remaining} seats left | ⏱️ {alert.hours_to_departure:.1f}h to dep", "short": True}
                    ],
                    "footer": "redBus Competitor Dynamic Pricing Intelligence",
                    "ts": int(datetime.now(timezone.utc).timestamp())
                }
            ]
        }

        try:
            req = urllib.request.Request(
                self.slack_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.warning(f"Failed to deliver Slack webhook: {e}")
            return False

    def send_telegram(self, alert: PriceAlert) -> bool:
        """Sends Markdown formatted alert message to Telegram Bot."""
        if not self.tg_token or not self.tg_chat_id:
            return False

        tg_url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        
        icon = "🔻" if "DROP" in alert.alert_type else "🔺" if "SURGE" in alert.alert_type else "🔥"
        msg_text = (
            f"{icon} *{alert.alert_type} DETECTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏢 *Operator:* `{alert.operator_name}`\n"
            f"📍 *Route:* `City {alert.from_city_id} ➔ {alert.to_city_id}`\n"
            f"📅 *DOJ:* `{alert.travel_date}` | 🕒 *Dep:* `{alert.departure_time}`\n"
            f"💰 *Old Fare:* ₹{int(alert.old_fare):,} ➔ *New:* ₹{int(alert.new_fare):,}\n"
            f"📊 *Change:* `{alert.fare_delta_pct:+.1f}%` ({alert.fare_delta:+,.0f} ₹)\n"
            f"💺 *Available Seats:* `{alert.seats_remaining}`\n"
            f"⏱️ *Hours to Departure:* `{alert.hours_to_departure:.1f}h`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _{alert.message}_"
        )

        data = urllib.parse.urlencode({
            "chat_id": self.tg_chat_id,
            "text": msg_text,
            "parse_mode": "Markdown"
        }).encode("utf-8")

        try:
            req = urllib.request.Request(tg_url, data=data)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Failed to deliver Telegram notification: {e}")
            return False


def detect_price_alerts(
    db_path: str = DEFAULT_DB_PATH,
    from_city_id: Optional[int] = None,
    to_city_id: Optional[int] = None,
    travel_date: Optional[str] = None,
    drop_threshold_pct: float = 10.0,
    surge_hours_limit: float = 24.0
) -> List[PriceAlert]:
    """
    Scans fare_snapshots in SQLite to find services with consecutive scrape observations.
    Fires alerts for:
      - Price drop >= drop_threshold_pct (default 10%)
      - Price increase within surge_hours_limit (default 24h)
      - Critical inventory <= 3 seats remaining inside 12h
    """
    with get_db_connection(db_path) as conn:
        filter_clause = "WHERE 1=1"
        params: List[Any] = []
        if from_city_id:
            filter_clause += " AND from_city_id = ?"
            params.append(from_city_id)
        if to_city_id:
            filter_clause += " AND to_city_id = ?"
            params.append(to_city_id)
        if travel_date:
            filter_clause += " AND travel_date = ?"
            params.append(travel_date)

        # Identify all services with multiple snapshots
        query = f"""
            SELECT service_id, COUNT(DISTINCT scraped_at) as snap_cnt
            FROM fare_snapshots
            {filter_clause}
            GROUP BY service_id
            HAVING snap_cnt >= 2;
        """
        multi_snap_services = pd.read_sql_query(query, conn, params=params)["service_id"].tolist()

        if not multi_snap_services:
            logger.info("No services with >= 2 snapshots found for alert evaluation.")
            return []

        logger.info(f"Evaluating price changes across {len(multi_snap_services)} tracked multi-snapshot services...")

        # Fetch recent 2 snapshots per service
        placeholders = ",".join("?" for _ in multi_snap_services)
        hist_query = f"""
            SELECT 
                snapshot_id, scraped_at, hours_to_departure, from_city_id, to_city_id,
                travel_date, operator_id, operator_name, service_id, bus_type,
                departure_time, effective_fare, seats_available
            FROM fare_snapshots
            WHERE service_id IN ({placeholders})
            ORDER BY service_id, scraped_at DESC;
        """
        hist_df = pd.read_sql_query(hist_query, conn, params=multi_snap_services)

    alerts: List[PriceAlert] = []

    for svc_id, group in hist_df.groupby("service_id"):
        if len(group) < 2:
            continue

        latest = group.iloc[0]
        previous = group.iloc[1]

        old_fare = float(previous["effective_fare"])
        new_fare = float(latest["effective_fare"])
        delta = new_fare - old_fare

        if old_fare <= 0:
            continue

        delta_pct = (delta / old_fare) * 100.0
        h_dep = float(latest["hours_to_departure"]) if pd.notnull(latest["hours_to_departure"]) else 999.0
        seats_left = int(latest["seats_available"]) if pd.notnull(latest["seats_available"]) else 0
        prev_seats = int(previous["seats_available"]) if pd.notnull(previous["seats_available"]) else 0

        # Deterministic alert fingerprint (avoids duplicate alerts for same snapshot comparison)
        seed_id = f"{svc_id}_{latest['scraped_at']}_{int(new_fare)}"

        # -------------------------------------------------------------
        # 1. Competitor Price Drop Alert (>= 10% drop)
        # -------------------------------------------------------------
        if delta_pct <= -abs(drop_threshold_pct):
            alert_id = f"ALT_DROP_{zlib.crc32(seed_id.encode('utf-8')):08x}"
            msg = (
                f"Competitor {latest['operator_name']} dropped fare by {abs(delta_pct):.1f}% "
                f"(₹{int(old_fare)} ➔ ₹{int(new_fare)}) on {latest['travel_date']} at {latest['departure_time']}. "
                f"Remaining inventory: {seats_left} seats."
            )
            alerts.append(PriceAlert(
                alert_id=alert_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                alert_type="COMPETITOR_PRICE_DROP",
                severity="HIGH",
                operator_id=int(latest["operator_id"]),
                operator_name=str(latest["operator_name"]),
                service_id=str(svc_id),
                from_city_id=int(latest["from_city_id"]),
                to_city_id=int(latest["to_city_id"]),
                travel_date=str(latest["travel_date"]),
                departure_time=str(latest["departure_time"]),
                hours_to_departure=h_dep,
                old_fare=old_fare,
                new_fare=new_fare,
                fare_delta=delta,
                fare_delta_pct=delta_pct,
                seats_remaining=seats_left,
                message=msg,
                dispatched_channels=""
            ))

        # -------------------------------------------------------------
        # 2. Last-Minute Surge Alert (within 24 hours of departure)
        # -------------------------------------------------------------
        elif delta_pct >= 5.0 and h_dep <= surge_hours_limit:
            alert_id = f"ALT_SURGE_{zlib.crc32(seed_id.encode('utf-8')):08x}"
            msg = (
                f"Last-minute price surge: {latest['operator_name']} increased fare by +{delta_pct:.1f}% "
                f"(₹{int(old_fare)} ➔ ₹{int(new_fare)}) within {h_dep:.1f}h of departure. "
                f"Remaining seats: {seats_left}."
            )
            alerts.append(PriceAlert(
                alert_id=alert_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                alert_type="LAST_MINUTE_SURGE",
                severity="WARNING",
                operator_id=int(latest["operator_id"]),
                operator_name=str(latest["operator_name"]),
                service_id=str(svc_id),
                from_city_id=int(latest["from_city_id"]),
                to_city_id=int(latest["to_city_id"]),
                travel_date=str(latest["travel_date"]),
                departure_time=str(latest["departure_time"]),
                hours_to_departure=h_dep,
                old_fare=old_fare,
                new_fare=new_fare,
                fare_delta=delta,
                fare_delta_pct=delta_pct,
                seats_remaining=seats_left,
                message=msg,
                dispatched_channels=""
            ))

        # -------------------------------------------------------------
        # 3. Imminent Sellout / Inventory Exhaustion Alert
        # -------------------------------------------------------------
        elif seats_left <= 3 and prev_seats > 3 and h_dep <= 12.0:
            alert_id = f"ALT_INVENTORY_{zlib.crc32(seed_id.encode('utf-8')):08x}"
            msg = (
                f"Inventory exhaustion alert: {latest['operator_name']} has only {seats_left} seats remaining "
                f"for service {svc_id} departing in {h_dep:.1f}h. Market demand is running at maximum load."
            )
            alerts.append(PriceAlert(
                alert_id=alert_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                alert_type="LOW_INVENTORY_SURGE",
                severity="CRITICAL",
                operator_id=int(latest["operator_id"]),
                operator_name=str(latest["operator_name"]),
                service_id=str(svc_id),
                from_city_id=int(latest["from_city_id"]),
                to_city_id=int(latest["to_city_id"]),
                travel_date=str(latest["travel_date"]),
                departure_time=str(latest["departure_time"]),
                hours_to_departure=h_dep,
                old_fare=old_fare,
                new_fare=new_fare,
                fare_delta=delta,
                fare_delta_pct=delta_pct,
                seats_remaining=seats_left,
                message=msg,
                dispatched_channels=""
            ))

    return alerts


def check_and_dispatch_alerts(
    db_path: str = DEFAULT_DB_PATH,
    from_city_id: Optional[int] = None,
    to_city_id: Optional[int] = None,
    travel_date: Optional[str] = None,
    drop_threshold_pct: float = 10.0,
    slack_webhook_url: Optional[str] = None,
    telegram_bot_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None
) -> int:
    """
    Executes the end-to-end alert workflow:
    1. Detects anomalies between snapshots
    2. Sends Slack & Telegram notifications if configured
    3. Persists records to SQLite price_alerts table
    4. Prints summary cards to console
    """
    alerts = detect_price_alerts(
        db_path=db_path,
        from_city_id=from_city_id,
        to_city_id=to_city_id,
        travel_date=travel_date,
        drop_threshold_pct=drop_threshold_pct
    )

    if not alerts:
        logger.info("✅ All competitor prices are stable. No alerts triggered.")
        return 0

    dispatcher = AlertDispatcher(
        slack_webhook_url=slack_webhook_url,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id
    )

    persisted_alerts: List[Dict[str, Any]] = []

    print("\n" + "=" * 80)
    print(f"🚨 COMPETITOR PRICE & SURGE ALERTS ({len(alerts)} DETECTED)")
    print("=" * 80)

    for a in alerts:
        channels = ["SQLITE"]

        # 1. Slack Webhook
        if dispatcher.slack_url:
            if dispatcher.send_slack(a):
                channels.append("SLACK")

        # 2. Telegram Bot
        if dispatcher.tg_token and dispatcher.tg_chat_id:
            if dispatcher.send_telegram(a):
                channels.append("TELEGRAM")

        a.dispatched_channels = ", ".join(channels)
        persisted_alerts.append(asdict(a))

        # 3. High-visibility Terminal Output
        badge = "🔻 [PRICE DROP]" if "DROP" in a.alert_type else "🔺 [SURGE]" if "SURGE" in a.alert_type else "🔥 [INVENTORY]"
        print(f"\n{badge} {a.operator_name} (Route: {a.from_city_id}➔{a.to_city_id} | DOJ: {a.travel_date} {a.departure_time})")
        print(f"   Fare: ₹{int(a.old_fare)} ➔ ₹{int(a.new_fare)} ({a.fare_delta_pct:+.1f}%) | Seats Remaining: {a.seats_remaining}")
        print(f"   Details: {a.message}")
        print(f"   Dispatched to: {a.dispatched_channels}")

    print("=" * 80 + "\n")

    # Persist in SQLite
    inserted = insert_price_alerts(persisted_alerts, db_path=db_path)
    logger.info(f"Persisted {inserted} price alerts to database table 'price_alerts'.")
    return len(alerts)


def main():
    parser = argparse.ArgumentParser(description="Competitor Dynamic Pricing Alerting & Notification Engine")
    parser.add_argument("--check", action="store_true", help="Run alert scan on latest snapshots")
    parser.add_argument("--from-id", type=int, default=None, help="Origin City ID filter")
    parser.add_argument("--to-id", type=int, default=None, help="Destination City ID filter")
    parser.add_argument("--date", type=str, default=None, help="Journey date filter (DD-MMM-YYYY)")
    parser.add_argument("--threshold", type=float, default=10.0, help="Price drop alert threshold percentage (default: 10.0)")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--test-alert", action="store_true", help="Send a mock test alert to verify Slack/Telegram webhooks")
    args = parser.parse_args()

    if args.test_alert:
        print("Sending simulated test alert to configured channels...")
        dispatcher = AlertDispatcher()
        mock_alert = PriceAlert(
            alert_id="ALT_TEST_001",
            created_at=datetime.now(timezone.utc).isoformat(),
            alert_type="COMPETITOR_PRICE_DROP",
            severity="HIGH",
            operator_id=999,
            operator_name="Mock Competitor Express",
            service_id="SVC_MOCK_TEST",
            from_city_id=733,
            to_city_id=807,
            travel_date="30-Oct-2026",
            departure_time="22:30",
            hours_to_departure=5.5,
            old_fare=750.0,
            new_fare=635.0,
            fare_delta=-115.0,
            fare_delta_pct=-15.3,
            seats_remaining=12,
            message="Mock Test: Competitor slashed fare by 15.3% ahead of prime departure.",
            dispatched_channels=""
        )

        slack_ok = dispatcher.send_slack(mock_alert)
        tg_ok = dispatcher.send_telegram(mock_alert)
        print(f"Results: Slack={slack_ok} (Webhook configured: {bool(dispatcher.slack_url)}), Telegram={tg_ok} (Token configured: {bool(dispatcher.tg_token)})")
        return

    check_and_dispatch_alerts(
        db_path=args.db_path,
        from_city_id=args.from_id,
        to_city_id=args.to_id,
        travel_date=args.date,
        drop_threshold_pct=args.threshold
    )


if __name__ == "__main__":
    main()
