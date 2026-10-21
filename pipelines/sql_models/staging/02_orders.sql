-- staging.orders: typed timestamps, normalised status, pseudonymised customer reference.
-- TRY_CAST turns an unparseable value into NULL; the hard checks (Pandera) have already
-- rejected unparseable purchase timestamps, so a NULL here means "genuinely missing".
CREATE OR REPLACE TABLE staging.orders AS
SELECT
    order_id,
    left(sha256('{{ salt }}' || ':cid:' || customer_id), 32)        AS customer_ref,
    lower(trim(order_status))                                        AS status,
    TRY_CAST(order_purchase_timestamp AS TIMESTAMP)                  AS purchase_ts,
    TRY_CAST(order_approved_at AS TIMESTAMP)                         AS approved_ts,
    TRY_CAST(order_delivered_carrier_date AS TIMESTAMP)              AS carrier_ts,
    TRY_CAST(order_delivered_customer_date AS TIMESTAMP)             AS delivered_ts,
    TRY_CAST(order_estimated_delivery_date AS TIMESTAMP)             AS estimated_ts
FROM raw.orders;
