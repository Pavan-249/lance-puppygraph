import argparse
import lancedb
from gremlin_python.driver import client as gremlin_client
from sentence_transformers import SentenceTransformer

LANCE_PATH     = str(__import__('pathlib').Path(__file__).parent / "lance_whales")
EMBED_MODEL    = "BAAI/bge-small-en-v1.5"
PUPPYGRAPH_URL = "ws://localhost:8183/gremlin"
TOP_K          = 5

def gremlin(g, query):
    return g.submit(query).all().result()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="calf behavior")
    args = parser.parse_args()

    print(f"\nQuery: '{args.query}'\n")

    model = SentenceTransformer(EMBED_MODEL)
    db    = lancedb.connect(LANCE_PATH)
    tbl   = db.open_table("sightings")

    matches = tbl.search(model.encode(args.query).tolist()).limit(TOP_K).to_pandas()

    print("Semantically matched sightings:")
    for _, row in matches.iterrows():
        print(f"  [Whale {row['whale_id']}] - {row['remarks'][:80]}...")

    whale_ids = [f"'Whale[{wid}]'" for wid in matches["whale_id"].unique().tolist()]
    ids_str   = ", ".join(whale_ids)

    g = gremlin_client.Client(PUPPYGRAPH_URL, "g")

    print("\nWhales that were matched directly:")
    direct = gremlin(g, f"g.V({ids_str}).values('whale_name').dedup()")
    for name in direct:
        print(f"  {name}")

    print("\nOther whales spotted in the same water at the same time (<=30 days):")
    connected = gremlin(g, f"""
        g.V({ids_str})
         .out('SHARES_LOCATION')
         .dedup()
         .project('name', 'locality')
           .by('whale_name')
           .by(__.in('SPOTTED_AT').values('locality').dedup().fold())
    """)
    for w in connected:
        locs = ", ".join(w['locality'])
        print(f"  {w['name']} (seen in {locs})")

    g.close()

if __name__ == "__main__":
    main()
