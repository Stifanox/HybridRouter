SELECT
  CAST(payment_type AS STRING) AS payment_type,
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
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
WHERE
  DATE(pickup_datetime) = CAST('2022-12-05' AS DATE)
GROUP BY
  payment_type
ORDER BY
  surcharge_to_revenue_roi_pct DESC
LIMIT 10
