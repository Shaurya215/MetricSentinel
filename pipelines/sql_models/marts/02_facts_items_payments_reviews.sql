-- fct_order_items, fct_payments, fct_reviews.

CREATE OR REPLACE TABLE marts.fct_order_items AS
SELECT
    i.order_id,
    i.item_seq,
    i.product_id,
    i.seller_id,
    p.category_en,
    i.price,
    i.freight,
    CAST(o.purchase_ts AS DATE)  AS purchase_date
FROM staging.order_items i
JOIN staging.orders o ON o.order_id = i.order_id
LEFT JOIN marts.dim_products p ON p.product_id = i.product_id;

CREATE OR REPLACE TABLE marts.fct_payments AS
SELECT order_id, payment_seq AS seq, payment_type, installments, value
FROM staging.order_payments;

-- fct_reviews: one row per order. comment_text is UNTRUSTED free text (NFR-011): it is
-- truncated here and must never be treated as an instruction by any consumer.
CREATE OR REPLACE TABLE marts.fct_reviews AS
SELECT
    order_id,
    review_id,
    CAST(created_ts AS DATE)                              AS review_date,
    score,
    comment_text IS NOT NULL                              AS has_comment,
    left(comment_text, {{ review_text_max_chars }})       AS comment_text
FROM staging.order_reviews;
