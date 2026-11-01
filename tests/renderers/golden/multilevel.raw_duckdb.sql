SELECT
  CAST(payment_type AS TEXT) AS payment_type,
  (
    SUM(total_amount) - SUM(fare_amount)
  ) - SUM(tip_amount) AS non_fare_surcharges,
  (
    (
      (
        SUM(total_amount) - SUM(fare_amount)
      ) - SUM(tip_amount)
    ) / NULLIF(SUM(total_amount), 0)
  ) * 100 AS surcharge_to_revenue_roi_pct
FROM raw_trips
WHERE
  CAST(pickup_datetime AS DATE) = CAST('2022-12-05' AS DATE)
GROUP BY
  CAST(payment_type AS TEXT)
ORDER BY
  surcharge_to_revenue_roi_pct DESC
LIMIT 10
