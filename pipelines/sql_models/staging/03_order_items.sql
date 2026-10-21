-- staging.order_items: typed. Money is DECIMAL(12,2) so sums are exact and the
-- warehouse content hash does not depend on floating-point summation order.
CREATE OR REPLACE TABLE staging.order_items AS
SELECT
    order_id,
    CAST(order_item_id AS INTEGER)                  AS item_seq,
    product_id,
    seller_id,
    TRY_CAST(shipping_limit_date AS TIMESTAMP)      AS shipping_limit_ts,
    CAST(price AS DECIMAL(12, 2))                   AS price,
    CAST(freight_value AS DECIMAL(12, 2))           AS freight
FROM raw.order_items;
