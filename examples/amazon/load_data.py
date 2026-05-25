import re
import itertools
import duckdb
import lancedb
import pandas as pd
from datasets import load_dataset
from sentence_transformers import SentenceTransformer

DATASET     = "gmongaras/Amazon-Reviews-2023"
DUCKDB_PATH = "amazon.db"
LANCE_PATH  = "./lance_amazon"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_SIZE  = 64
MAX_REVIEWS = 40000


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_')[:80]


print(f"Streaming {MAX_REVIEWS} reviews from HuggingFace...")
ds     = load_dataset(DATASET, split="train", streaming=True)
rows   = list(itertools.islice(ds, MAX_REVIEWS))
rev_df = pd.DataFrame(rows)
rev_df = rev_df[rev_df["text"].notna() & (rev_df["text"].str.len() > 20)].copy()
rev_df["product_id"] = rev_df["parent_asin"].fillna(rev_df["asin"])
print(f"  {len(rev_df)} reviews with text")

# keep only reviewers with 2+ reviews so graph traversal has depth
freq = rev_df["user_id"].value_counts()
rev_df = rev_df[rev_df["user_id"].isin(freq[freq >= 2].index)].copy()
print(f"  {len(rev_df)} reviews after filtering to multi-reviewers")

reviewers_df    = pd.DataFrame({"user_id": rev_df["user_id"].unique()})
products_df     = pd.DataFrame({"product_id": rev_df["product_id"].unique()})
reviews_edge_df = rev_df[["user_id", "product_id", "rating"]].drop_duplicates(["user_id", "product_id"]).copy()
reviews_edge_df["rating"] = reviews_edge_df["rating"].fillna(0).astype(str)

print(f"\n  {len(reviewers_df)} reviewers | {len(products_df)} products | {len(reviews_edge_df)} edges")

print("\nWriting DuckDB...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")
for tbl in ["public.reviews", "public.products", "public.reviewers"]:
    con.execute(f"DROP TABLE IF EXISTS {tbl}")

con.execute("CREATE TABLE public.reviewers (user_id VARCHAR)")
for uid in reviewers_df["user_id"].tolist():
    con.execute("INSERT INTO public.reviewers VALUES (?)", [uid])

con.execute("CREATE TABLE public.products (product_id VARCHAR)")
for pid in products_df["product_id"].tolist():
    con.execute("INSERT INTO public.products VALUES (?)", [pid])

con.execute("CREATE TABLE public.reviews (user_id VARCHAR, product_id VARCHAR, rating VARCHAR)")
for _, r in reviews_edge_df.iterrows():
    con.execute("INSERT INTO public.reviews VALUES (?,?,?)", [r["user_id"], r["product_id"], r["rating"]])

con.close()

print(f"\nEmbedding {len(rev_df)} review texts...")
model  = SentenceTransformer(EMBED_MODEL)
texts  = rev_df["text"].tolist()
vectors = []
for i in range(0, len(texts), BATCH_SIZE):
    batch = texts[i:i + BATCH_SIZE]
    vectors.extend(model.encode(batch, show_progress_bar=False).tolist())
    print(f"  {min(i + BATCH_SIZE, len(texts))}/{len(texts)}")

embed_df = rev_df[["user_id", "product_id", "text", "rating"]].copy()
embed_df["rating"]  = embed_df["rating"].fillna(0).astype(str)
embed_df["vector"]  = vectors

lance_db = lancedb.connect(str(__import__('pathlib').Path(__file__).parent / "lance_amazon"))
lance_db.create_table("reviews", data=embed_df.to_dict("records"), mode="overwrite")

print(f"\nDone. DuckDB: {DUCKDB_PATH} | LanceDB: lance_amazon/")
