import argparse
import lancedb
from gremlin_python.driver import client as gremlin_client
from sentence_transformers import SentenceTransformer

LANCE_PATH     = str(__import__('pathlib').Path(__file__).parent / "lance_imdb")
EMBED_MODEL    = "BAAI/bge-small-en-v1.5"
PUPPYGRAPH_URL = "ws://localhost:8183/gremlin"
TOP_K          = 5


def gremlin(g, query):
    return g.submit(query).all().result()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="a man who discovers he has been living in a simulated reality")
    args = parser.parse_args()

    print(f"\nQuery: '{args.query}'\n")

    model = SentenceTransformer(EMBED_MODEL)
    db    = lancedb.connect(LANCE_PATH)
    tbl   = db.open_table("movies")

    matches = tbl.search(model.encode(args.query).tolist()).limit(TOP_K).to_pandas()

    print("Semantically matched movies:")
    for _, row in matches.iterrows():
        print(f"  {row['title']}")

    movie_ids = [f"'Movie[{mid}]'" for mid in matches["movie_id"].tolist()]
    ids_str   = ", ".join(movie_ids)

    g = gremlin_client.Client(PUPPYGRAPH_URL, "g")

    print("\nActors who appear across the most of these films:")
    actor_counts = gremlin(g, f"""
        g.V({ids_str})
         .out('FEATURES')
         .values('name')
         .groupCount()
         .order(local).by(values, desc)
         .limit(local, 5)
    """)
    for name, count in actor_counts[0].items():
        print(f"  {name} ({count} {'film' if count == 1 else 'films'})")

    print("\nDirectors behind these films:")
    directors = gremlin(g, f"""
        g.V({ids_str})
         .out('DIRECTED_BY')
         .values('name')
         .dedup()
    """)
    for d in directors:
        print(f"  {d}")

    print("\nGenres these films span:")
    genres = gremlin(g, f"""
        g.V({ids_str})
         .out('HAS_GENRE')
         .values('name')
         .dedup()
    """)
    print(f"  {', '.join(genres)}")

    seed = matches["movie_id"].iloc[0]
    print(f"\nOther movies featuring actors from '{matches['title'].iloc[0]}':")
    recs = gremlin(g, f"""
        g.V('Movie[{seed}]')
         .out('FEATURES')
         .in('FEATURES')
         .values('title')
         .dedup()
         .limit(8)
    """)
    for title in recs:
        print(f"  {title}")

    g.close()


if __name__ == "__main__":
    main()
