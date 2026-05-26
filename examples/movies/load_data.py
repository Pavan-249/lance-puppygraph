import hashlib
import duckdb
import lancedb
import pandas as pd
from datasets import load_dataset
from sentence_transformers import SentenceTransformer

DUCKDB_PATH = "movies.db"
LANCE_PATH  = "./lance_movies"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

def gen_id(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

print("Loading HuggingFace TMDB Movies Dataset...")
ds = load_dataset('CohereLabs/movies', split='train')

movies = []
actors = {}
acts_in = []
producers = {}
produced_by = []

for i, row in enumerate(ds):
    title = row.get("title")
    overview = row.get("overview")
    if not title or not overview:
        continue
    
    movie_id = gen_id(title)
    
    movies.append({
        "movie_id": movie_id,
        "title": title,
        "overview": overview,
        "genres": row.get("genres", "")
    })
    
    # Parse Producers
    prod_str = row.get("producer", "")
    if prod_str:
        for p_str in prod_str.split(", "):
            name = p_str.strip()
            if not name: continue
            prod_id = gen_id("prod_" + name)
            if prod_id not in producers:
                producers[prod_id] = {"producer_id": prod_id, "name": name}
            produced_by.append({"movie_id": movie_id, "producer_id": prod_id})
    
    # Parse Actors
    cast_str = row.get("cast", "")
    if cast_str:
        # e.g., "Sam Worthington as Jake Sully, Zoe Saldana as Neytiri"
        for actor_str in cast_str.split(", "):
            parts = actor_str.split(" as ")
            name = parts[0].strip()
            role = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                continue
                
            actor_id = gen_id("actor_" + name)
            if actor_id not in actors:
                actors[actor_id] = {"actor_id": actor_id, "name": name}
                
            acts_in.append({
                "movie_id": movie_id,
                "actor_id": actor_id,
                "role": role
            })

movies_df = pd.DataFrame(movies).drop_duplicates("movie_id")
actors_df = pd.DataFrame(list(actors.values()))
acts_in_df = pd.DataFrame(acts_in).drop_duplicates(["movie_id", "actor_id"])
producers_df = pd.DataFrame(list(producers.values()))
produced_by_df = pd.DataFrame(produced_by).drop_duplicates(["movie_id", "producer_id"])

print(f"Parsed {len(movies_df)} movies, {len(actors_df)} actors, {len(producers_df)} producers.")

print("\\nBuilding DuckDB Graph...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")
for t in ["public.movies", "public.actors", "public.acts_in", "public.producers", "public.produced_by"]:
    con.execute(f"DROP TABLE IF EXISTS {t}")

con.execute("CREATE TABLE public.movies AS SELECT * FROM movies_df")
con.execute("CREATE TABLE public.actors AS SELECT * FROM actors_df")
con.execute("CREATE TABLE public.acts_in AS SELECT * FROM acts_in_df")
con.execute("CREATE TABLE public.producers AS SELECT * FROM producers_df")
con.execute("CREATE TABLE public.produced_by AS SELECT * FROM produced_by_df")
con.close()
print("DuckDB complete.")

print(f"\nBuilding LanceDB Vector Store (Model: {EMBED_MODEL})...")
model = SentenceTransformer(EMBED_MODEL)

print("Embedding movie plots...")
vectors = []
BATCH = 128
for i in range(0, len(movies_df), BATCH):
    batch = movies_df["overview"].iloc[i:i+BATCH].tolist()
    vectors.extend(model.encode(batch, show_progress_bar=False).tolist())
    print(f"  {min(i+BATCH, len(movies_df))}/{len(movies_df)}")

movies_df["vector"] = vectors

ldb = lancedb.connect(LANCE_PATH)
ldb.create_table("movies", data=movies_df.to_dict("records"), mode="overwrite")
print("LanceDB complete.")

print("\nNext steps:")
print("1. Start PuppyGraph pointing to movies.db")
print("2. Run python demo_server.py")
