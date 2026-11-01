SELECT
  pickup_date,
  rate_code,
  SUM(total_fare_amount) AS total_fare_amount,
  SUM(total_revenue) / NULLIF(SUM(total_distance_miles), 0) AS avg_revenue_per_mile
FROM READ_PARQUET('./data/nyc_taxi/pickup_date=*/rollup.parquet', hive_partitioning = TRUE)
WHERE
  pickup_date IN (CAST('2022-12-01' AS DATE), CAST('2022-12-02' AS DATE))
  AND vendor_id IN ('1', '2')
  AND NOT payment_type IN ('3', '4')
  AND rate_code <> '99'
GROUP BY
  pickup_date,
  rate_code
