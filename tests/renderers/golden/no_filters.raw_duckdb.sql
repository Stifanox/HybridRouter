SELECT
  COUNT(1) AS trips_count,
  SUM(total_amount) / NULLIF(SUM(trip_distance), 0) AS avg_revenue_per_mile
FROM raw_trips
