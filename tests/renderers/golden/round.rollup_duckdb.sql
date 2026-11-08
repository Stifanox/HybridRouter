SELECT
  vendor_id,
  ROUND(SUM(total_fare_amount) / NULLIF(COALESCE(SUM(trips_count), 0), 0), 2) AS avg_fare_per_trip
FROM READ_PARQUET(
  [
    './data/nyc_taxi/pickup_date=2022-12-01/rollup-00000001.parquet',
    './data/nyc_taxi/pickup_date=2022-12-02/rollup-00000002.parquet'
  ],
  hive_partitioning = TRUE
)
WHERE
  pickup_date > CAST('2022-11-30' AS DATE)
  AND pickup_date < CAST('2022-12-24' AS DATE)
GROUP BY
  vendor_id
ORDER BY
  vendor_id ASC
