SELECT
  pickup_date,
  vendor_id,
  SUM(trips_count) AS trips_count,
  SUM(total_revenue) AS total_revenue,
  (
    SUM(total_tip_amount) / NULLIF(SUM(total_fare_amount), 0)
  ) * 100 AS tip_to_fare_ratio_pct,
  SUM(total_distance_miles) / NULLIF((
    SUM(total_duration_seconds) / 3600.0
  ), 0) AS avg_trip_speed_mph
FROM READ_PARQUET('./data/nyc_taxi/pickup_date=*/rollup.parquet', hive_partitioning = TRUE)
WHERE
  pickup_date >= CAST('2022-12-01' AS DATE)
  AND pickup_date <= CAST('2022-12-23' AS DATE)
  AND payment_type = '1'
GROUP BY
  pickup_date,
  vendor_id
ORDER BY
  pickup_date ASC
LIMIT 1000
