"""
Whale demo data loader
Pulls real named-individual sightings from Happywhale via GBIF public API.
Only keeps records with actual observer remarks (not "(no remarks)").
Builds DuckDB graph + LanceDB embeddings.

Usage: python load_data.py
"""
import json, re, time, requests, duckdb, lancedb, pandas as pd
from sentence_transformers import SentenceTransformer

GBIF_BASE   = "https://api.gbif.org/v1/occurrence/search"
DATASET_KEY = "6cb6ab2b-b5ac-4134-9b25-574dcfcbef09"
DUCKDB_PATH = "whale.db"
LANCE_PATH  = "./lance_whales"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Fetch from Alaska + Hawaii (where named whales are densest in this dataset)
REGIONS = [
    {"name": "Alaska",  "lat": "55,62",  "lon": "-160,-125"},
    {"name": "Hawaii",  "lat": "18,23",  "lon": "-162,-154"},
    {"name": "BC",      "lat": "48,55",  "lon": "-132,-122"},
]


def parse_remarks_blob(raw: str):
    """Extract organism_name and occurrence_remarks from the embedded JSON."""
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None, None
        blob = json.loads(m.group())
        name    = blob.get("organism_name", "").strip()
        remarks = blob.get("occurrence_remarks", "").strip()
        org_id  = blob.get("organism_id", "").strip()
        return name, remarks, org_id
    except Exception:
        return None, None, None


def fetch_region(lat, lon, max_pages=6):
    records = []
    offset  = 0
    limit   = 300
    for _ in range(max_pages):
        r = requests.get(GBIF_BASE, params={
            "datasetKey":       DATASET_KEY,
            "scientificName":   "Megaptera novaeangliae",
            "decimalLatitude":  lat,
            "decimalLongitude": lon,
            "limit":            limit,
            "offset":           offset,
        }, timeout=20).json()

        for rec in r.get("results", []):
            raw = rec.get("occurrenceRemarks", "")
            name, remarks, org_id = parse_remarks_blob(raw)
            if not name:
                continue
            if not remarks or remarks in ("(no remarks)", "", "Photo identification"):
                continue
            # strip "Photo identification; " prefix from remarks if present
            if remarks.startswith("Photo identification"):
                remarks = remarks.split(";", 1)[-1].strip()
            if not remarks:
                continue

            records.append({
                "sighting_id": str(rec.get("gbifID", "")),
                "whale_id":    org_id or name,
                "whale_name":  name,
                "date":        (rec.get("eventDate") or "")[:10],
                "lat":         rec.get("decimalLatitude"),
                "lon":         rec.get("decimalLongitude"),
                "locality":    rec.get("locality") or rec.get("stateProvince", ""),
                "observer":    rec.get("rightsHolder") or rec.get("recordedBy", ""),
                "remarks":     remarks,
                "happywhale":  rec.get("catalogNumber", ""),
            })

        if r.get("endOfRecords", True):
            break
        offset += limit
        time.sleep(0.3)   # be polite to the API

    return records


print("Fetching sightings from GBIF Happywhale dataset...")
all_records = []
for region in REGIONS:
    print(f"  {region['name']}...", end=" ", flush=True)
    recs = fetch_region(region["lat"], region["lon"])
    print(f"{len(recs)} sightings with remarks")
    all_records.extend(recs)

df = pd.DataFrame(all_records).drop_duplicates("sighting_id")
print(f"\nTotal: {len(df)} sightings | {df['whale_id'].nunique()} unique whales")

if df.empty:
    print("No data fetched. Check API or network.")
    raise SystemExit(1)

# ── DuckDB ──────────────────────────────────────────────────────────────────
print("\nBuilding DuckDB...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")
for t in ["public.shared_location", "public.sightings", "public.whales"]:
    con.execute(f"DROP TABLE IF EXISTS {t}")

# whales
whales_df = (
    df[["whale_id", "whale_name", "happywhale"]]
    .drop_duplicates("whale_id")
    .rename(columns={"happywhale": "happywhale_url"})
)
con.execute("CREATE TABLE public.whales AS SELECT * FROM whales_df")
print(f"  whales: {len(whales_df)} rows")

# sightings
sightings_df = df[["sighting_id", "whale_id", "date", "lat", "lon",
                    "locality", "observer", "remarks"]].copy()
con.execute("CREATE TABLE public.sightings AS SELECT * FROM sightings_df")
print(f"  sightings: {len(sightings_df)} rows")

# shared_location: two named whales spotted in the same locality AND within 30 days
# (real connection from the data — not fabricated)
merged = df[["whale_id", "locality", "date"]].merge(
    df[["whale_id", "locality", "date"]], on="locality", suffixes=("_a", "_b")
)
merged = merged[merged["whale_id_a"] < merged["whale_id_b"]]
merged["date_a"] = pd.to_datetime(merged["date_a"], errors="coerce")
merged["date_b"] = pd.to_datetime(merged["date_b"], errors="coerce")
merged["days_diff"] = (merged["date_a"] - merged["date_b"]).dt.days.abs()
filtered = merged[merged["days_diff"] <= 30]

shared = (
    filtered.groupby(["whale_id_a", "whale_id_b", "locality"])
    .size()
    .reset_index(name="co_sightings")
    [["whale_id_a", "whale_id_b", "locality", "co_sightings"]]
)
con.execute("CREATE TABLE public.shared_location AS SELECT * FROM shared")
print(f"  shared_location edges: {len(shared)} pairs (tightened <= 30 days)")
con.close()

# ── LanceDB ─────────────────────────────────────────────────────────────────
print(f"\nEmbedding {len(df)} sighting remarks with {EMBED_MODEL}...")
model = SentenceTransformer(EMBED_MODEL)

BATCH = 128
vectors = []
for i in range(0, len(df), BATCH):
    batch = df["remarks"].iloc[i:i+BATCH].tolist()
    vectors.extend(model.encode(batch, show_progress_bar=False).tolist())
    print(f"  {min(i+BATCH, len(df))}/{len(df)}")

embed_df = sightings_df[["sighting_id", "whale_id", "remarks"]].copy()
embed_df["vector"] = vectors

ldb = lancedb.connect(LANCE_PATH)
ldb.create_table("sightings", data=embed_df.to_dict("records"), mode="overwrite")
print(f"LanceDB table created: {len(embed_df)} sighting embeddings")

print("\nDone.")
print(f"  DuckDB:  {DUCKDB_PATH}")
print(f"  LanceDB: {LANCE_PATH}/")
print(f"\nNext: python ../../fusion_proxy.py --config config.yaml")
