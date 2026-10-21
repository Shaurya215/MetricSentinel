-- staging.order_reviews: exactly one review per order.
-- Source issues handled here: several reviews for one order, and the same review_id
-- reused for different orders. Rule: keep the latest review per order, ordered by answer
-- timestamp, then creation timestamp, then review_id so ties are broken deterministically.
-- The counts before and after are written to the quality report.
CREATE OR REPLACE TABLE staging.order_reviews AS
WITH parsed AS (
    SELECT
        review_id,
        order_id,
        CAST(review_score AS INTEGER)                          AS score,
        nullif(trim(review_comment_title), '')                 AS comment_title,
        nullif(trim(review_comment_message), '')               AS comment_text,
        TRY_CAST(review_creation_date AS TIMESTAMP)            AS created_ts,
        TRY_CAST(review_answer_timestamp AS TIMESTAMP)         AS answered_ts
    FROM raw.order_reviews
),
ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY order_id
            ORDER BY answered_ts DESC NULLS LAST, created_ts DESC NULLS LAST, review_id DESC
        ) AS rn
    FROM parsed
)
SELECT review_id, order_id, score, comment_title, comment_text, created_ts, answered_ts
FROM ranked
WHERE rn = 1;
