# Databricks notebook source
# MAGIC %md
# MAGIC # Compute urgent parcels for today's route plan
# MAGIC
# MAGIC Derives `urgent_parcels_today` from the synthetic `parcels` + `customers`
# MAGIC tables produced by `00_setup_synthetic_data`. This is the **input table**
# MAGIC that both routing surfaces consume:
# MAGIC
# MAGIC - the Lakeflow Designer **Route Optimizer** operator (PR B), and
# MAGIC - the Genie **`optimize_routes` TVF** (PR C).
# MAGIC
# MAGIC The urgency rule: keep parcels created on/before today that are **due
# MAGIC within the planning horizon**, then join customer lat/lon so the row is
# MAGIC self-contained as solver input. Output columns are exactly what the
# MAGIC solver core expects per stop: `parcel_id, lat, lon, demand_units,
# MAGIC ready_minute, due_minute`.

# COMMAND ----------

dbutils.widgets.text("catalog", "supply_chain", "Catalog")
dbutils.widgets.text("schema", "route_optimizer_accelerator", "Schema")
# Horizon: a parcel is "urgent" if its due_minute falls within the horizon.
# horizon_days selects which parcels (by created_date) are in scope for today;
# horizon_minutes is the within-shift cutoff applied to due_minute.
dbutils.widgets.text("horizon_days", "1", "Horizon (days)")
dbutils.widgets.text("horizon_minutes", "480", "Horizon (minutes within shift)")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
HORIZON_DAYS = int(dbutils.widgets.get("horizon_days"))
HORIZON_MINUTES = int(dbutils.widgets.get("horizon_minutes"))

spark.sql(f"USE {CATALOG}.{SCHEMA}")

print(
    f"Using {CATALOG}.{SCHEMA}; horizon = {HORIZON_DAYS} day(s), "
    f"due within {HORIZON_MINUTES} min of shift start"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build `urgent_parcels_today`
# MAGIC
# MAGIC - `created_date` within the last `horizon_days` (inclusive of today).
# MAGIC - `due_minute <= horizon_minutes` — only parcels that must move inside
# MAGIC   the planning window; the rest wait for a later run.
# MAGIC - Joined to `customers` for the stop's `lat`/`lon`.
# MAGIC
# MAGIC Written as a Delta table (not a view) so the operator and TVF read a
# MAGIC stable snapshot for today's plan.

# COMMAND ----------

urgent = spark.sql(f"""
    SELECT
      p.parcel_id,
      c.lat,
      c.lon,
      p.demand_units,
      p.ready_minute,
      p.due_minute
    FROM parcels p
    JOIN customers c USING (customer_id)
    WHERE p.created_date >= DATE_SUB(CURRENT_DATE(), {HORIZON_DAYS - 1})
      AND p.created_date <= CURRENT_DATE()
      AND p.due_minute <= {HORIZON_MINUTES}
    ORDER BY p.due_minute, p.parcel_id
""")

(
    urgent.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable("urgent_parcels_today")
)

n = spark.table("urgent_parcels_today").count()
print(f"urgent_parcels_today: {n} parcels in scope for today's plan")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preview

# COMMAND ----------

display(spark.sql("""
    SELECT parcel_id, lat, lon, demand_units, ready_minute, due_minute
    FROM urgent_parcels_today
    ORDER BY due_minute, parcel_id
    LIMIT 20
"""))
