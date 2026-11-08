SELECT
  COALESCE(SUM(trips_count), 0) AS trips_count,
  SUM(total_revenue) / NULLIF(SUM(total_distance_miles), 0) AS avg_revenue_per_mile
FROM READ_PARQUET(
  [
    './data/nyc_taxi/pickup_date=2022-12-01/rollup-00000001.parquet',
    './data/nyc_taxi/pickup_date=2022-12-02/rollup-00000002.parquet'
  ],
  hive_partitioning = TRUE
)
