SELECT
  CAST(vendor_id AS STRING) AS vendor_id,
  ROUND(SUM(fare_amount) / NULLIF(COUNT(1), 0), 2) AS avg_fare_per_trip
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
WHERE
  DATE(pickup_datetime) > CAST('2022-11-30' AS DATE)
  AND DATE(pickup_datetime) < CAST('2022-12-24' AS DATE)
GROUP BY
  vendor_id
ORDER BY
  vendor_id ASC NULLS LAST
