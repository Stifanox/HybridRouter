SELECT
  COUNT(1) AS trips_count,
  SUM(total_amount) / NULLIF(SUM(trip_distance), 0) AS avg_revenue_per_mile
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
