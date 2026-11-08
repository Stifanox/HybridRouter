SELECT
  payment_type,
  (
    SUM(total_revenue) - SUM(total_fare_amount)
  ) - SUM(total_tip_amount) AS non_fare_surcharges,
  (
    (
      (
        SUM(total_revenue) - SUM(total_fare_amount)
      ) - SUM(total_tip_amount)
    ) / NULLIF(SUM(total_revenue), 0)
  ) * 100 AS surcharge_to_revenue_roi_pct
FROM READ_PARQUET(
  [
    './data/nyc_taxi/pickup_date=2022-12-01/rollup-00000001.parquet',
    './data/nyc_taxi/pickup_date=2022-12-02/rollup-00000002.parquet'
  ],
  hive_partitioning = TRUE
)
WHERE
  pickup_date = CAST('2022-12-05' AS DATE)
GROUP BY
  payment_type
ORDER BY
  surcharge_to_revenue_roi_pct DESC
LIMIT 10
