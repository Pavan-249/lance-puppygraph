"""
Beauty Products Example — Data Loader
======================================

Creates the graph data (DuckDB) and vector embeddings (LanceDB) for
a beauty product → ingredient knowledge graph.

Data source: OpenBeautyFacts via HuggingFace.

Usage:
    cd examples/beauty
    python load_data.py

This creates:
    - beauty.db       (DuckDB file with products, ingredients, product_ingredients tables)
    - ./lance_data/   (LanceDB directory with ingredient embeddings)

After loading data, start the proxy:
    python ../../fusion_proxy.py --config config.yaml
"""

import duckdb
import lancedb
import pandas as pd
from datasets import load_dataset
from sentence_transformers import SentenceTransformer

DUCKDB_PATH = "beauty.db"
LANCE_PATH = "./lance_data"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# ── Step 1: Download dataset ────────────────────────────────────────────

print("Step 1: Loading OpenBeautyFacts from HuggingFace...")
ds = load_dataset(
    "openfoodfacts/product-database",
    split="beauty",
    trust_remote_code=True,
)
df = ds.to_pandas()
print(f"Loaded {len(df)} products")

# Keep only rows with ingredient data
df = df[df["ingredients_text"].notna() & df["product_name"].notna()].copy()
df = df[["code", "product_name", "brands", "categories_tags",
         "ingredients_text", "labels_tags"]].copy()
df = df.rename(columns={"code": "product_id"})
df["product_id"] = df["product_id"].astype(str)
print(f"After filtering: {len(df)} products with ingredient data")

# ── Step 2: Build DuckDB graph tables ───────────────────────────────────

print("\nStep 2: Building DuckDB graph tables...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")

con.execute("DROP TABLE IF EXISTS public.product_ingredients")
con.execute("DROP TABLE IF EXISTS public.products")
con.execute("DROP TABLE IF EXISTS public.ingredients")

con.execute("""
    CREATE TABLE public.products AS
    SELECT product_id, product_name, brands, categories_tags, labels_tags
    FROM df
""")
print(f"Created products table: "
      f"{con.execute('SELECT COUNT(*) FROM public.products').fetchone()[0]} rows")

print("Parsing ingredients (this takes a minute)...")
ingredient_rows = []
edge_rows = []
for _, row in df.iterrows():
    if not row["ingredients_text"]:
        continue
    raw = str(row["ingredients_text"])
    ingredients = [i.strip().lower() for i in raw.split(",") if len(i.strip()) > 2]
    for ing in ingredients:
        ing_id = ing.replace(" ", "_")[:80]
        ingredient_rows.append({
            "ingredient_id": ing_id,
            "ingredient_name": ing[:120],
        })
        edge_rows.append({
            "product_id": str(row["product_id"]),
            "ingredient_id": ing_id,
        })

ingredients_df = pd.DataFrame(ingredient_rows).drop_duplicates(subset=["ingredient_id"])
con.execute("CREATE TABLE public.ingredients AS SELECT * FROM ingredients_df")
print(f"Created ingredients table: {len(ingredients_df)} unique ingredients")

edges_df = pd.DataFrame(edge_rows)
con.execute("CREATE TABLE public.product_ingredients AS SELECT * FROM edges_df")
print(f"Created product_ingredients edge table: {len(edges_df)} edges")
con.close()

# ── Step 3: Embed ingredients into LanceDB ──────────────────────────────

print(f"\nStep 3: Embedding {len(ingredients_df)} ingredients into LanceDB...")
model = SentenceTransformer(EMBED_MODEL)

ingredient_names = ingredients_df["ingredient_name"].tolist()
BATCH_SIZE = 256
embeddings = []
for i in range(0, len(ingredient_names), BATCH_SIZE):
    batch = ingredient_names[i:i + BATCH_SIZE]
    vecs = model.encode(batch, show_progress_bar=False)
    embeddings.extend(vecs.tolist())
    if i % 2000 == 0:
        print(f"  {i}/{len(ingredient_names)}")

ingredients_df = ingredients_df.copy()
ingredients_df["vector"] = embeddings

lance_db = lancedb.connect(LANCE_PATH)
lance_db.create_table(
    "ingredients",
    data=ingredients_df[["ingredient_id", "ingredient_name", "vector"]],
    mode="overwrite",
)
print(f"LanceDB table created with {len(ingredients_df)} ingredient embeddings")
print(f"\nDone! Now run: python ../../fusion_proxy.py --config config.yaml")
