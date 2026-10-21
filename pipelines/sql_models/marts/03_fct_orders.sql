-- fct_orders: one row per order.
--
-- Definitions (see DATA_PLAN.md sections 3 and 5):
--   is_delivered   status = 'delivered' AND a delivery timestamp exists. A "delivered"
--                  order with no delivery date is NOT counted as delivered (it is counted
--                  in the quality report instead).
--   is_late        delivered on a later calendar DATE than the estimated date.
--   delivery_days  fractional days from purchase to delivery.
--   order_gmv      sum of item prices; NULL when the order has no items, so such orders
--                  drop out of GMV and AOV instead of counting as zero-value orders.
--   primary_payment_type
--                  the payment with the largest value (ties: lowest sequence number).
CREATE OR REPLACE TABLE marts.fct_orders AS
WITH item_totals AS (
    SELECT
        order_id,
        count(*)       AS item_count,
        sum(price)     AS order_gmv,
        sum(freight)   AS freight_total
    FROM staging.order_items
    GROUP BY order_id
),
payment_ranked AS (
    SELECT
        order_id,
        payment_type,
        value,
        row_number() OVER (PARTITION BY order_id ORDER BY value DESC, payment_seq ASC) AS rn,
        count(*) OVER (PARTITION BY order_id) AS payment_count,
        sum(value) OVER (PARTITION BY order_id) AS payment_total
    FROM staging.order_payments
),
primary_payment AS (
    SELECT order_id, payment_type AS primary_payment_type, payment_count, payment_total
    FROM payment_ranked
    WHERE rn = 1
)
SELECT
    o.order_id,
    c.customer_key,
    c.state                                                       AS customer_state,
    CAST(o.purchase_ts AS DATE)                                   AS purchase_date,
    o.purchase_ts,
    o.approved_ts                                                 AS approved_at,
    CAST(o.delivered_ts AS DATE)                                  AS delivered_date,
    CAST(o.estimated_ts AS DATE)                                  AS estimated_date,
    o.status,
    (o.status = 'delivered' AND o.delivered_ts IS NOT NULL)       AS is_delivered,
    (o.status = 'delivered' AND o.delivered_ts IS NOT NULL
        AND CAST(o.delivered_ts AS DATE) > CAST(o.estimated_ts AS DATE)) AS is_late,
    CASE
        WHEN o.status = 'delivered' AND o.delivered_ts IS NOT NULL
        THEN (epoch(o.delivered_ts) - epoch(o.purchase_ts)) / 86400.0
    END                                                           AS delivery_days,
    coalesce(t.item_count, 0)                                     AS item_count,
    t.order_gmv,
    t.freight_total,
    t.order_id IS NOT NULL                                        AS has_items,
    p.order_id IS NOT NULL                                        AS has_payment,
    p.primary_payment_type,
    coalesce(p.payment_count, 0)                                  AS payment_count
FROM staging.orders o
LEFT JOIN staging.customers c   ON c.customer_ref = o.customer_ref
LEFT JOIN item_totals t         ON t.order_id = o.order_id
LEFT JOIN primary_payment p     ON p.order_id = o.order_id;
