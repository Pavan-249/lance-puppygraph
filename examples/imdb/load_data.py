import json
import re
import duckdb
import lancedb
import pandas as pd
from datasets import load_dataset
from sentence_transformers import SentenceTransformer

DATASET     = "AiresPucrs/tmdb-5000-movies"
DUCKDB_PATH = "imdb.db"
LANCE_PATH  = "./lance_imdb"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_SIZE  = 64
MAX_CAST    = 5


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_')[:80]


def parse_cast(s):
    try:
        return [c["name"] for c in json.loads(s)[:MAX_CAST] if "name" in c]
    except Exception:
        return []


def parse_directors(s):
    try:
        return [c["name"] for c in json.loads(s) if c.get("job") == "Director"]
    except Exception:
        return []


def parse_genres(s):
    try:
        return [g["name"] for g in json.loads(s) if "name" in g]
    except Exception:
        return []


print("Downloading dataset from HuggingFace...")
df = load_dataset(DATASET, split="train").to_pandas()
df = df[df["overview"].notna() & (df["overview"].str.len() > 20)].copy()
df["movie_id"] = df["title"].apply(slugify)
df["year"]     = df["release_date"].apply(lambda x: str(x)[:4] if pd.notna(x) else "")
df["rating"]   = df["vote_average"].apply(lambda x: str(round(x, 1)) if pd.notna(x) else "")
print(f"{len(df)} movies loaded")

movies_df = df[["movie_id", "title", "year", "rating", "overview"]].drop_duplicates("movie_id").copy()

director_rows, movie_director_rows = [], []
for _, r in df.iterrows():
    for d in parse_directors(r.get("crew", "[]")):
        did = slugify(d)
        director_rows.append({"director_id": did, "name": d})
        movie_director_rows.append({"movie_id": r["movie_id"], "director_id": did})

directors_df       = pd.DataFrame(director_rows).drop_duplicates("director_id")
movie_directors_df = pd.DataFrame(movie_director_rows).drop_duplicates()

actor_rows, movie_actor_rows = [], []
for _, r in df.iterrows():
    for a in parse_cast(r.get("cast", "[]")):
        aid = slugify(a)
        actor_rows.append({"actor_id": aid, "name": a})
        movie_actor_rows.append({"movie_id": r["movie_id"], "actor_id": aid})

actors_df       = pd.DataFrame(actor_rows).drop_duplicates("actor_id")
movie_actors_df = pd.DataFrame(movie_actor_rows).drop_duplicates()

genre_rows, movie_genre_rows = [], []
for _, r in df.iterrows():
    for g in parse_genres(r.get("genres", "[]")):
        gid = slugify(g)
        genre_rows.append({"genre_id": gid, "name": g})
        movie_genre_rows.append({"movie_id": r["movie_id"], "genre_id": gid})

genres_df       = pd.DataFrame(genre_rows).drop_duplicates("genre_id")
movie_genres_df = pd.DataFrame(movie_genre_rows).drop_duplicates()

print(f"  {len(movies_df)} movies | {len(directors_df)} directors | {len(actors_df)} actors | {len(genres_df)} genres")
print(f"  {len(movie_directors_df)} director edges | {len(movie_actors_df)} actor edges | {len(movie_genres_df)} genre edges")

print("\nWriting DuckDB...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")
for tbl in ["public.movie_genres", "public.movie_actors", "public.movie_directors",
            "public.actors", "public.directors", "public.genres", "public.movies"]:
    con.execute(f"DROP TABLE IF EXISTS {tbl}")

con.execute("CREATE TABLE public.movies (movie_id VARCHAR, title VARCHAR, year VARCHAR, rating VARCHAR, overview VARCHAR)")
for _, r in movies_df.iterrows():
    con.execute("INSERT INTO public.movies VALUES (?,?,?,?,?)",
                [r["movie_id"], r["title"], r["year"], r["rating"], r["overview"]])

con.execute("CREATE TABLE public.directors (director_id VARCHAR, name VARCHAR)")
for _, r in directors_df.iterrows():
    con.execute("INSERT INTO public.directors VALUES (?,?)", [r["director_id"], r["name"]])

con.execute("CREATE TABLE public.actors (actor_id VARCHAR, name VARCHAR)")
for _, r in actors_df.iterrows():
    con.execute("INSERT INTO public.actors VALUES (?,?)", [r["actor_id"], r["name"]])

con.execute("CREATE TABLE public.genres (genre_id VARCHAR, name VARCHAR)")
for _, r in genres_df.iterrows():
    con.execute("INSERT INTO public.genres VALUES (?,?)", [r["genre_id"], r["name"]])

con.execute("CREATE TABLE public.movie_directors (movie_id VARCHAR, director_id VARCHAR)")
for _, r in movie_directors_df.iterrows():
    con.execute("INSERT INTO public.movie_directors VALUES (?,?)", [r["movie_id"], r["director_id"]])

con.execute("CREATE TABLE public.movie_actors (movie_id VARCHAR, actor_id VARCHAR)")
for _, r in movie_actors_df.iterrows():
    con.execute("INSERT INTO public.movie_actors VALUES (?,?)", [r["movie_id"], r["actor_id"]])

con.execute("CREATE TABLE public.movie_genres (movie_id VARCHAR, genre_id VARCHAR)")
for _, r in movie_genres_df.iterrows():
    con.execute("INSERT INTO public.movie_genres VALUES (?,?)", [r["movie_id"], r["genre_id"]])

con.close()

print(f"\nEmbedding {len(movies_df)} movie plots with {EMBED_MODEL}...")
model    = SentenceTransformer(EMBED_MODEL)
overviews = movies_df["overview"].tolist()
vectors  = []
for i in range(0, len(overviews), BATCH_SIZE):
    batch = overviews[i:i + BATCH_SIZE]
    vectors.extend(model.encode(batch, show_progress_bar=False).tolist())
    print(f"  {min(i + BATCH_SIZE, len(overviews))}/{len(overviews)}")

embed_df = movies_df[["movie_id", "title", "overview"]].copy()
embed_df["vector"] = vectors

lance_db = lancedb.connect(str(__import__('pathlib').Path(__file__).parent / "lance_imdb"))
lance_db.create_table("movies", data=embed_df.to_dict("records"), mode="overwrite")

print(f"\nDone. DuckDB: {DUCKDB_PATH} | LanceDB: lance_imdb/")
