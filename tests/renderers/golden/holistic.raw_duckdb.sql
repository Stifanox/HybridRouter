SELECT
  CAST(pickup_datetime AS DATE) AS pickup_date,
  COUNT(1) AS trips_count,
  COUNT(DISTINCT pickup_location_id) AS unique_pickup_zones
FROM raw_trips
WHERE
  CAST(pickup_datetime AS DATE) >= CAST('2022-12-01' AS DATE)
  AND CAST(pickup_datetime AS DATE) <= CAST('2022-12-07' AS DATE)
GROUP BY
  CAST(pickup_datetime AS DATE)
ORDER BY
  unique_pickup_zones DESC
LIMIT 5
