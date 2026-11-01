SELECT
  DATE(pickup_datetime) AS pickup_date,
  CAST(rate_code AS STRING) AS rate_code,
  SUM(fare_amount) AS total_fare_amount,
  SUM(total_amount) / NULLIF(SUM(trip_distance), 0) AS avg_revenue_per_mile
FROM bigquery-public-data.new_york_taxi_trips.tlc_yellow_trips_2022
WHERE
  DATE(pickup_datetime) IN (CAST('2022-12-01' AS DATE), CAST('2022-12-02' AS DATE))
  AND CAST(vendor_id AS STRING) IN ('1', '2')
  AND NOT CAST(payment_type AS STRING) IN ('3', '4')
  AND CAST(rate_code AS STRING) <> '99'
GROUP BY
  DATE(pickup_datetime),
  CAST(rate_code AS STRING)
