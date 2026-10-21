-- staging.sellers / products / category_translation.
-- The source misspells "length" as "lenght"; the correction happens here, raw keeps it.
-- The seller zip prefix is dropped (zip prefixes are not exposed downstream).
CREATE OR REPLACE TABLE staging.sellers AS
SELECT
    seller_id,
    nullif(lower(trim(seller_city)), '')    AS city,
    nullif(upper(trim(seller_state)), '')   AS state
FROM raw.sellers;

CREATE OR REPLACE TABLE staging.category_translation AS
SELECT
    lower(trim(product_category_name))           AS category_pt,
    lower(trim(product_category_name_english))   AS category_en
FROM raw.category_translation;

CREATE OR REPLACE TABLE staging.products AS
SELECT
    product_id,
    nullif(lower(trim(product_category_name)), '')   AS category_pt,
    TRY_CAST(product_name_lenght AS INTEGER)          AS name_length,
    TRY_CAST(product_description_lenght AS INTEGER)   AS description_length,
    TRY_CAST(product_photos_qty AS INTEGER)           AS photos_qty,
    TRY_CAST(product_weight_g AS DOUBLE)              AS weight_g,
    TRY_CAST(product_length_cm AS DOUBLE)             AS length_cm,
    TRY_CAST(product_height_cm AS DOUBLE)             AS height_cm,
    TRY_CAST(product_width_cm AS DOUBLE)              AS width_cm
FROM raw.products;
