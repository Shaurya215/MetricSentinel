-- staging.customers: one row per Olist customer_id (which is really "customer per order").
-- Both identifiers are replaced by salted hashes (DATA_PLAN.md section 12); the zip
-- prefix is dropped on purpose. customer_ref joins to orders; customer_key identifies the
-- person across orders (customer_unique_id in the source).
CREATE OR REPLACE TABLE staging.customers AS
SELECT
    left(sha256('{{ salt }}' || ':cid:' || customer_id), 32)        AS customer_ref,
    left(sha256('{{ salt }}' || ':uid:' || customer_unique_id), 32) AS customer_key,
    nullif(lower(trim(customer_city)), '')                           AS city,
    nullif(upper(trim(customer_state)), '')                          AS state
FROM raw.customers;
