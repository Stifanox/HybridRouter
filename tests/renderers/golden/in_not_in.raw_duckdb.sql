SELECT
  CAST(pickup_datetime AS DATE) AS pickup_date,
  CAST(rate_code AS TEXT) AS rate_code,
  SUM(fare_amount) AS total_fare_amount,
  SUM(total_amount) / NULLIF(SUM(trip_distance), 0) AS avg_revenue_per_mile
FROM raw_trips
WHERE
  CAST(pickup_datetime AS DATE) IN (CAST('2022-12-01' AS DATE), CAST('2022-12-02' AS DATE))
  AND CAST(vendor_id AS TEXT) IN ('1', '2')
  AND NOT CAST(payment_type AS TEXT) IN ('3', '4')
  AND CAST(rate_code AS TEXT) <> '99'
GROUP BY
  CAST(pickup_datetime AS DATE),
  CAST(rate_code AS TEXT)
