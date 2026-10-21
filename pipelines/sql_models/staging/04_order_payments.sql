-- staging.order_payments: typed. `not_defined` is kept as its own payment type.
CREATE OR REPLACE TABLE staging.order_payments AS
SELECT
    order_id,
    CAST(payment_sequential AS INTEGER)             AS payment_seq,
    lower(trim(payment_type))                       AS payment_type,
    CAST(payment_installments AS INTEGER)           AS installments,
    CAST(payment_value AS DECIMAL(12, 2))           AS value
FROM raw.order_payments;
