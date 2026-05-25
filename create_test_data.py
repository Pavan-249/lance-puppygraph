import duckdb
import lancedb
from sentence_transformers import SentenceTransformer

DUCKDB_PATH = "test_data.db"
LANCE_PATH  = "./test_lance_data"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

genres = [
    {"genre_id": "horror",    "genre_desc": "horror, scary, dark, monsters, terror, jump scares, supernatural evil"},
    {"genre_id": "comedy",    "genre_desc": "comedy, funny, humor, laughs, jokes, light-hearted, silly"},
    {"genre_id": "scifi",     "genre_desc": "sci-fi, space, future, technology, robots, aliens, dystopia"},
    {"genre_id": "romance",   "genre_desc": "romance, love, relationships, passion, heartbreak, dating"},
    {"genre_id": "thriller",  "genre_desc": "thriller, suspense, tension, mystery, crime, danger, plot twists"},
    {"genre_id": "drama",     "genre_desc": "drama, emotional, serious, character study, conflict, human struggles"},
    {"genre_id": "action",    "genre_desc": "action, explosions, fights, chases, heroes, adrenaline, stunts"},
    {"genre_id": "animation", "genre_desc": "animation, animated, cartoon, family-friendly, colorful, imaginative"},
]

movies = [
    ("the_shining",          "The Shining",               "Stanley Kubrick"),
    ("superbad",             "Superbad",                  "Greg Mottola"),
    ("interstellar",         "Interstellar",              "Christopher Nolan"),
    ("the_notebook",         "The Notebook",              "Nick Cassavetes"),
    ("se7en",                "Se7en",                     "David Fincher"),
    ("the_godfather",        "The Godfather",             "Francis Ford Coppola"),
    ("mad_max_fury_road",    "Mad Max: Fury Road",        "George Miller"),
    ("spirited_away",        "Spirited Away",             "Hayao Miyazaki"),
    ("get_out",              "Get Out",                   "Jordan Peele"),
    ("grand_budapest_hotel", "The Grand Budapest Hotel",  "Wes Anderson"),
]

edges = [
    ("the_shining", "horror"),         ("the_shining", "thriller"),
    ("superbad", "comedy"),
    ("interstellar", "scifi"),         ("interstellar", "drama"),
    ("the_notebook", "romance"),       ("the_notebook", "drama"),
    ("se7en", "thriller"),             ("se7en", "drama"),
    ("the_godfather", "drama"),        ("the_godfather", "thriller"),
    ("mad_max_fury_road", "action"),   ("mad_max_fury_road", "scifi"),
    ("spirited_away", "animation"),    ("spirited_away", "drama"),
    ("get_out", "horror"),             ("get_out", "thriller"),  ("get_out", "comedy"),
    ("grand_budapest_hotel", "comedy"),("grand_budapest_hotel", "drama"),
]

print("Loading embedding model...")
model = SentenceTransformer(EMBED_MODEL)

for g in genres:
    g["vector"] = model.encode(g["genre_desc"]).tolist()

print("Writing LanceDB...")
lance_db = lancedb.connect(LANCE_PATH)
lance_db.create_table("genres", data=genres, mode="overwrite")

print("Writing DuckDB...")
con = duckdb.connect(DUCKDB_PATH)
con.execute("CREATE SCHEMA IF NOT EXISTS public")
con.execute("DROP TABLE IF EXISTS public.movie_genres")
con.execute("DROP TABLE IF EXISTS public.movies")
con.execute("DROP TABLE IF EXISTS public.genres")

con.execute("CREATE TABLE public.genres (genre_id VARCHAR, genre_desc VARCHAR)")
for g in genres:
    con.execute("INSERT INTO public.genres VALUES (?, ?)", [g["genre_id"], g["genre_desc"]])

con.execute("CREATE TABLE public.movies (movie_id VARCHAR, title VARCHAR, director VARCHAR)")
for m in movies:
    con.execute("INSERT INTO public.movies VALUES (?, ?, ?)", list(m))

con.execute("CREATE TABLE public.movie_genres (movie_id VARCHAR, genre_id VARCHAR)")
for e in edges:
    con.execute("INSERT INTO public.movie_genres VALUES (?, ?)", list(e))

con.close()
print(f"Done. DuckDB: {DUCKDB_PATH} | LanceDB: {LANCE_PATH}")
print(f"Next: venv/bin/python fusion_proxy.py --config config.yaml")
