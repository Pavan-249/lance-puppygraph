import duckdb

DUCKDB_PATH = "whale.db"

print("Connecting to DuckDB...")
con = duckdb.connect(DUCKDB_PATH)

print("Dropping old shared_location table...")
con.execute("DROP TABLE IF EXISTS public.shared_location")

print("Building new shared_location table (same locality AND within 30 days)...")
con.execute("""
    CREATE TABLE public.shared_location AS
    SELECT s1.whale_id AS whale_id_a, s2.whale_id AS whale_id_b, s1.locality, COUNT(*) AS co_sightings
    FROM public.sightings s1
    JOIN public.sightings s2 ON s1.locality = s2.locality
    WHERE s1.whale_id < s2.whale_id
      AND abs(epoch(cast(s1.date as DATE)) - epoch(cast(s2.date as DATE))) <= 30 * 24 * 60 * 60
    GROUP BY s1.whale_id, s2.whale_id, s1.locality
""")

count = con.execute("SELECT COUNT(*) FROM public.shared_location").fetchone()[0]
print(f"Successfully rebuilt shared_location table with {count} edges.")

con.close()
