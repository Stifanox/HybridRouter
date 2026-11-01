SELECT
  CAST(vendor_id AS TEXT) AS vendor_id,
  ROUND(SUM(fare_amount) / NULLIF(COUNT(1), 0), 2) AS avg_fare_per_trip
FROM raw_trips
WHERE
  CAST(pickup_datetime AS DATE) > CAST('2022-11-30' AS DATE)
  AND CAST(pickup_datetime AS DATE) < CAST('2022-12-24' AS DATE)
GROUP BY
  CAST(vendor_id AS TEXT)
ORDER BY
  vendor_id ASC
