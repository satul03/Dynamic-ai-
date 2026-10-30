-- ==============================================================================
-- Analytics Queries for redBus Competitor Dynamic Pricing & Market Intelligence
-- Database: bus_pricing_intelligence.db
-- ==============================================================================

-- 1. Operator Price Benchmarking & Market Share
-- Aggregates total services, seat capacity, min/median/max fares, and discount percentage per operator
SELECT 
    s.operator_id,
    s.operator_name,
    o.is_rtc,
    o.is_primo,
    COUNT(s.service_id) AS total_services,
    SUM(s.seats_available) AS total_available_seats,
    ROUND(MIN(s.effective_fare), 2) AS min_effective_fare,
    ROUND(AVG(s.effective_fare), 2) AS avg_effective_fare,
    ROUND(MAX(s.effective_fare), 2) AS max_effective_fare,
    ROUND(AVG(COALESCE(s.discount_pct, 0)), 1) AS avg_discount_pct,
    ROUND(AVG(s.rating), 2) AS avg_customer_rating,
    SUM(s.review_count) AS rating_count,
    SUM(s.review_count) AS total_reviews
FROM fare_snapshots s
LEFT JOIN operators o ON s.operator_id = o.operator_id
GROUP BY s.operator_id, s.operator_name
ORDER BY total_services DESC, min_effective_fare ASC;


-- 2. RTC (State Government Transport) vs Private Competitor Fares
-- Compares public RTC services against private coach operators
SELECT 
    CASE WHEN o.is_rtc = 1 THEN 'RTC (Government)' ELSE 'Private Operator' END AS operator_category,
    COUNT(DISTINCT s.operator_id) AS unique_operators,
    COUNT(s.service_id) AS total_buses,
    ROUND(MIN(s.effective_fare), 2) AS lowest_fare,
    ROUND(AVG(s.effective_fare), 2) AS mean_fare,
    ROUND(MAX(s.effective_fare), 2) AS highest_fare,
    ROUND(AVG(s.seats_available), 1) AS avg_seats_remaining,
    ROUND(AVG(s.rating), 2) AS avg_rating,
    SUM(s.review_count) AS total_rating_count
FROM fare_snapshots s
LEFT JOIN operators o ON s.operator_id = o.operator_id
GROUP BY CASE WHEN o.is_rtc = 1 THEN 'RTC (Government)' ELSE 'Private Operator' END;


-- 3. Bus Class / Coach Segmentation (AC Sleeper vs Seater vs Non-AC)
-- Dynamic pricing breakdown by comfort tier
SELECT 
    CASE 
        WHEN s.is_ac = 1 AND s.is_sleeper = 1 THEN 'AC Sleeper'
        WHEN s.is_ac = 1 AND s.is_sleeper = 0 THEN 'AC Seater'
        WHEN s.is_ac = 0 AND s.is_sleeper = 1 THEN 'Non-AC Sleeper'
        ELSE 'Non-AC Seater'
    END AS bus_category,
    COUNT(s.service_id) AS count_services,
    ROUND(MIN(s.effective_fare), 2) AS min_fare,
    ROUND(AVG(s.effective_fare), 2) AS avg_fare,
    ROUND(MAX(s.effective_fare), 2) AS max_fare,
    ROUND(AVG(s.seats_available), 1) AS avg_seats_left
FROM fare_snapshots s
GROUP BY bus_category
ORDER BY avg_fare DESC;


-- 4. Dynamic Pricing vs Departure Window (Hours to Departure)
-- Measures dynamic price surges as departure time nears
SELECT 
    CASE 
        WHEN s.hours_to_departure < 6 THEN '0-6 hrs (Last Minute)'
        WHEN s.hours_to_departure < 12 THEN '6-12 hrs (Same Day)'
        WHEN s.hours_to_departure < 24 THEN '12-24 hrs (1 Day Ahead)'
        WHEN s.hours_to_departure < 72 THEN '24-72 hrs (2-3 Days Ahead)'
        ELSE '72+ hrs (Advance Booking)'
    END AS departure_window,
    COUNT(s.snapshot_id) AS snapshot_count,
    ROUND(AVG(s.effective_fare), 2) AS avg_effective_fare,
    ROUND(AVG(s.discount_pct), 1) AS avg_discount_pct,
    ROUND(AVG(s.seats_available), 1) AS avg_available_seats
FROM fare_snapshots s
GROUP BY departure_window
ORDER BY MIN(s.hours_to_departure) ASC;


-- 5. redBus Primo Quality Premium
-- Evaluates the pricing premium commanded by Primo-certified operators
SELECT 
    CASE WHEN o.is_primo = 1 THEN 'Primo Certified' ELSE 'Standard Operator' END AS primo_status,
    COUNT(s.service_id) AS total_buses,
    ROUND(AVG(s.effective_fare), 2) AS avg_fare,
    ROUND(AVG(s.rating), 2) AS avg_rating,
    ROUND(AVG(s.seats_available), 1) AS avg_seats_available
FROM fare_snapshots s
LEFT JOIN operators o ON s.operator_id = o.operator_id
GROUP BY primo_status;


-- 6. Top Promotional Deals & Discount Tags
-- Identifies top discounting operators and marketing badges
SELECT 
    s.deal_tag,
    COUNT(s.service_id) AS buses_with_deal,
    ROUND(AVG(s.discount_pct), 1) AS avg_discount_pct,
    ROUND(AVG(s.original_fare - s.effective_fare), 2) AS avg_rupees_saved
FROM fare_snapshots s
WHERE s.deal_tag IS NOT NULL AND s.deal_tag != ''
GROUP BY s.deal_tag
ORDER BY buses_with_deal DESC
LIMIT 10;
