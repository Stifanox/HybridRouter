SELECT
  CAST(pickup_datetime AS DATE) AS pickup_date,
  CAST(vendor_id AS TEXT) AS vendor_id,
  COUNT(1) AS trips_count,
  SUM(total_amount) AS total_revenue,
  (
    SUM(tip_amount) / NULLIF(SUM(fare_amount), 0)
  ) * 100 AS tip_to_fare_ratio_pct,
  SUM(trip_distance) / NULLIF((
    SUM(DATE_DIFF('SECOND', pickup_datetime, dropoff_datetime)) / 3600.0
  ), 0) AS avg_trip_speed_mph
FROM raw_trips
WHERE
  CAST(pickup_datetime AS DATE) >= CAST('2022-12-01' AS DATE)
  AND CAST(pickup_datetime AS DATE) <= CAST('2022-12-23' AS DATE)
  AND CAST(payment_type AS TEXT) = '1'
GROUP BY
  CAST(pickup_datetime AS DATE),
  CAST(vendor_id AS TEXT)
ORDER BY
  pickup_date ASC
LIMIT 1000
