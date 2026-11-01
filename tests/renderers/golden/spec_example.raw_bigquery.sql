SELECT
  DATE(pickup_datetime) AS pickup_date,
  CAST(vendor_id AS STRING) AS vendor_id,
  COUNT(1) AS trips_count,
  SUM(total_amount) AS total_revenue,
  (
    SUM(tip_amount) / NULLIF(SUM(fare_amount), 0)
  ) * 100 AS tip_to_fare_ratio_pct,
  SUM(trip_distance) / NULLIF((
    SUM(TIMESTAMP_DIFF(dropoff_datetime, pickup_datetime, SECOND)) / 3600.0
  ), 0) AS avg_trip_speed_mph
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
WHERE
  DATE(pickup_datetime) >= CAST('2022-12-01' AS DATE)
  AND DATE(pickup_datetime) <= CAST('2022-12-23' AS DATE)
  AND CAST(payment_type AS STRING) = '1'
GROUP BY
  pickup_date,
  vendor_id
ORDER BY
  pickup_date ASC NULLS LAST
LIMIT 1000
