-- Dimension tables.

-- dim_products: category_en is the English name; a NULL category or one with no
-- translation both map to 'unknown' (DATA_PLAN.md section 3). category_pt keeps the
-- original so the two cases can still be told apart.
CREATE OR REPLACE TABLE marts.dim_products AS
SELECT
    p.product_id,
    coalesce(t.category_en, 'unknown')                  AS category_en,
    p.category_pt,
    p.weight_g,
    p.length_cm * p.height_cm * p.width_cm              AS volume_cm3
FROM staging.products p
LEFT JOIN staging.category_translation t ON t.category_pt = p.category_pt;

CREATE OR REPLACE TABLE marts.dim_sellers AS
SELECT seller_id, state, city
FROM staging.sellers;

-- dim_customers: one row per person (customer_key). State and city come from the
-- person's latest order. Order-level analysis should use fct_orders.customer_state,
-- which is the state at the time of that order.
CREATE OR REPLACE TABLE marts.dim_customers AS
WITH ranked AS (
    SELECT
        c.customer_key,
        c.state,
        c.city,
        row_number() OVER (
            PARTITION BY c.customer_key
            ORDER BY o.purchase_ts DESC NULLS LAST, c.customer_ref DESC
        ) AS rn
    FROM staging.customers c
    LEFT JOIN staging.orders o ON o.customer_ref = c.customer_ref
)
SELECT customer_key, state, city
FROM ranked
WHERE rn = 1;
