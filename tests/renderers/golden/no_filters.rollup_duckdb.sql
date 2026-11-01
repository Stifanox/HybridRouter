SELECT
  SUM(trips_count) AS trips_count,
  SUM(total_revenue) / NULLIF(SUM(total_distance_miles), 0) AS avg_revenue_per_mile
FROM READ_PARQUET('./data/nyc_taxi/pickup_date=*/rollup.parquet', hive_partitioning = TRUE)
