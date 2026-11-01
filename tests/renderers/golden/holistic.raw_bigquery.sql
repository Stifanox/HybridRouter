SELECT
  DATE(pickup_datetime) AS pickup_date,
  COUNT(1) AS trips_count,
  COUNT(DISTINCT pickup_location_id) AS unique_pickup_zones
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
WHERE
  DATE(pickup_datetime) >= CAST('2022-12-01' AS DATE)
  AND DATE(pickup_datetime) <= CAST('2022-12-07' AS DATE)
GROUP BY
  pickup_date
ORDER BY
  unique_pickup_zones DESC
LIMIT 5
