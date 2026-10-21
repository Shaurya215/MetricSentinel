-- dim_date: one row per calendar day from the first purchase to the last date that
-- appears anywhere in the data (including estimated delivery dates and review dates).
-- staging.br_holidays is loaded from the `holidays` package before the marts run.
CREATE OR REPLACE TABLE marts.dim_date AS
WITH bounds AS (
    SELECT
        min(purchase_date) AS first_day,
        greatest(
            max(purchase_date),
            max(delivered_date),
            max(estimated_date),
            coalesce((SELECT max(review_date) FROM marts.fct_reviews), max(purchase_date))
        ) AS last_day
    FROM marts.fct_orders
),
days AS (
    SELECT CAST(d AS DATE) AS date
    FROM bounds, generate_series(first_day, last_day, INTERVAL 1 DAY) AS t(d)
)
SELECT
    days.date,
    isodow(days.date)                  AS weekday,
    dayname(days.date)                 AS weekday_name,
    weekofyear(days.date)              AS week,
    month(days.date)                   AS month,
    year(days.date)                    AS year,
    isodow(days.date) >= 6             AS is_weekend,
    h.date IS NOT NULL                 AS is_holiday_br,
    h.name                             AS holiday_name
FROM days
LEFT JOIN staging.br_holidays h ON h.date = days.date;
