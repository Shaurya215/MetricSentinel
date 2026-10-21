-- seller_tier_daily: POINT-IN-TIME seller tier (DATA_PLAN.md section 5).
--
-- For each seller and day D, tier is the quartile of the seller's GMV over the
-- {{ tier_window_days }} days BEFORE D (D-{{ tier_window_days }} .. D-1, day D itself excluded). Using only the past
-- means a sale on D cannot influence its own tier, so no future information leaks
-- into the past.
--   tier 4 = top quartile of GMV among sellers with GMV in the window, tier 1 = bottom.
--   'unrated' = no GMV in the window (for example a seller's first sales).
-- Rows exist for every day from each seller's first sale to the last purchase date.
-- Ties in GMV are broken by seller_id so the result is deterministic.
CREATE OR REPLACE TABLE marts.seller_tier_daily AS
WITH seller_day AS (
    SELECT seller_id, purchase_date AS date, sum(price) AS gmv
    FROM marts.fct_order_items
    GROUP BY seller_id, purchase_date
),
bounds AS (
    SELECT max(date) AS last_day FROM seller_day
),
first_sale AS (
    SELECT seller_id, min(date) AS first_day FROM seller_day GROUP BY seller_id
),
spine AS (
    SELECT f.seller_id, CAST(d AS DATE) AS date
    FROM first_sale f, bounds b, generate_series(f.first_day, b.last_day, INTERVAL 1 DAY) AS t(d)
),
rolling AS (
    SELECT
        s.seller_id,
        s.date,
        sum(coalesce(sd.gmv, 0)) OVER (
            PARTITION BY s.seller_id
            ORDER BY s.date
            ROWS BETWEEN {{ tier_window_days }} PRECEDING AND 1 PRECEDING
        ) AS trailing_gmv
    FROM spine s
    LEFT JOIN seller_day sd ON sd.seller_id = s.seller_id AND sd.date = s.date
),
ranked AS (
    SELECT
        seller_id,
        date,
        trailing_gmv,
        CASE WHEN trailing_gmv > 0 THEN
            ntile(4) OVER (
                PARTITION BY date, (trailing_gmv > 0)
                ORDER BY trailing_gmv ASC, seller_id ASC
            )
        END AS quartile
    FROM rolling
)
SELECT
    seller_id,
    date,
    coalesce(trailing_gmv, 0) AS trailing_gmv,
    CASE WHEN quartile IS NULL THEN 'unrated' ELSE 'T' || CAST(quartile AS VARCHAR) END AS tier
FROM ranked;
