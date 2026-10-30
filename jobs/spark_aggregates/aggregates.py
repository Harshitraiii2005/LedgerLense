"""Nightly Spark batch: transaction aggregates -> MongoDB.

Reads ledgerlens.transactions, writes:
  agg_daily_category_spend   spend + count per (day, category, currency)   [Spark SQL]
  agg_merchant_flagged_rate  total / flagged / flagged_rate per merchant    [DataFrame API]
  agg_top_customers          customers ranked by txn count, amounts per currency

Runs inside the apache/spark image (Python 3.10, so no 3.11+ syntax here):
  spark-submit --master local[2] aggregates.py
Env: MONGO_URI (required), MONGO_DB (default ledgerlens), TOP_N (default 100).
Kubernetes: run image ledgerlens/spark-aggregates (Dockerfile here, default CMD) as a CronJob,
e.g. schedule "0 2 * * *", with MONGO_URI from a Secret and ~2Gi memory limit.
"""

import os
from datetime import datetime, timezone

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DecimalType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)  # fmt: skip
from pyspark.sql.window import Window

MONEY = DecimalType(18, 2)

# Explicit schema instead of sampling-based inference: deterministic types, and `risk` (only present
# once the risk service has scored a txn) is simply null when missing rather than absent from the schema.
TRANSACTIONS_SCHEMA = StructType([
    StructField("_id", StringType()),
    StructField("customer_id", StringType()),
    StructField("merchant", StringType()),
    StructField("category", StringType()),
    StructField("amount", MONEY),
    StructField("currency", StringType()),
    StructField("country", StringType()),
    StructField("channel", StringType()),
    StructField("timestamp", TimestampType()),
    StructField("risk", StructType([
        StructField("score", IntegerType()),
        StructField("flagged", BooleanType()),
        StructField("reasons", ArrayType(StructType([StructField("code", StringType())]))),
    ])),
])  # fmt: skip


def daily_category_spend(spark: SparkSession, txns: DataFrame) -> DataFrame:
    txns.createOrReplaceTempView("txns")
    # Grouped by currency too: summing USD and JPY together would be meaningless.
    return spark.sql("""
        SELECT to_date(timestamp)              AS day,
               category,
               currency,
               COUNT(*)                        AS txn_count,
               CAST(SUM(amount) AS DECIMAL(18,2)) AS total_amount
        FROM txns
        GROUP BY to_date(timestamp), category, currency
    """)


def merchant_flagged_rate(txns: DataFrame) -> DataFrame:
    is_flagged = F.coalesce(F.col("risk.flagged"), F.lit(False))  # unscored = not flagged
    return (
        txns.groupBy("merchant")
        .agg(F.count("*").alias("total_txns"), F.sum(is_flagged.cast("int")).alias("flagged_txns"))
        .withColumn("flagged_rate", F.round(F.col("flagged_txns") / F.col("total_txns"), 4))
    )


def top_customers(txns: DataFrame, n: int) -> DataFrame:
    per_currency = txns.groupBy("customer_id", "currency").agg(
        F.count("*").alias("cnt"), F.sum("amount").cast(MONEY).alias("total_amount")
    )
    per_customer = per_currency.groupBy("customer_id").agg(
        F.sum("cnt").alias("txn_count"),
        F.sort_array(F.collect_list(F.struct("currency", "total_amount"))).alias("amounts"),
    )
    # row_number, not rank: exactly n rows even with ties; customer_id breaks ties deterministically.
    # Trade-off: unpartitioned window = one partition (Spark warns). Fine for one row per customer;
    # switch to orderBy().limit(n) + zipWithIndex if the customer count reaches tens of millions.
    w = Window.orderBy(F.desc("txn_count"), F.asc("customer_id"))
    return per_customer.withColumn("rank", F.row_number().over(w)).filter(F.col("rank") <= n)


def main() -> None:
    uri, db = os.environ["MONGO_URI"], os.environ.get("MONGO_DB", "ledgerlens")
    spark = (
        SparkSession.builder.appName("ledgerlens-aggregates")
        .config("spark.sql.session.timeZone", "UTC")  # day boundaries in UTC, matching stored timestamps
        .config("spark.mongodb.read.connection.uri", uri)
        .config("spark.mongodb.write.connection.uri", uri)
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    txns = (
        spark.read.format("mongodb").schema(TRANSACTIONS_SCHEMA)
        .option("database", db).option("collection", "transactions").load()
        .cache()
    )  # fmt: skip
    computed_at = F.lit(datetime.now(timezone.utc)).cast(TimestampType())  # noqa: UP017 (py3.10 image)
    outputs = {
        "agg_daily_category_spend": daily_category_spend(spark, txns),
        "agg_merchant_flagged_rate": merchant_flagged_rate(txns),
        "agg_top_customers": top_customers(txns, int(os.environ.get("TOP_N", "100"))),
    }
    for collection, df in outputs.items():
        # Trade-off: full recompute + overwrite each night (idempotent, simple). Switch to incremental
        # per-day upserts once a full scan of transactions no longer fits the batch window.
        (df.withColumn("computed_at", computed_at).write.format("mongodb").mode("overwrite")
           .option("database", db).option("collection", collection).save())  # fmt: skip
        print(f"wrote {collection}: {df.count()} rows")
    spark.stop()


if __name__ == "__main__":
    main()
