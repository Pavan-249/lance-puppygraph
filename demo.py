"""
Movies Graph Demo
=================
Shows LanceDB and PuppyGraph working together perfectly!

Usage:
  python demo.py --vibe "dark scary nightmares"
"""
import argparse
import lancedb
from gremlin_python.driver import client as gremlin_client
from sentence_transformers import SentenceTransformer

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vibe", default="space exploration adventure")
    args = parser.parse_args()

    print(f"\n1. VIBE CHECK: '{args.vibe}'")
    
    # 1. Ask LanceDB for semantic matches
    model = SentenceTransformer("BAAI/bge-small-en-v1.5")
    db = lancedb.connect("./test_lance_data")
    tbl = db.open_table("genres")
    
    vec = model.encode(args.vibe).tolist()
    results = tbl.search(vec).limit(2).to_pandas()
    
    genre_ids = results["genre_id"].tolist()
    print(f"   LanceDB found matching genres: {genre_ids}")

    # 2. Ask PuppyGraph for the connected movies
    print(f"\n2. GRAPH TRAVERSAL: Finding movies connected to those genres...")
    
    # Format IDs for Gremlin: 'Genre[scifi]', 'Genre[action]'
    formatted_ids = ", ".join(f"'Genre[{gid}]'" for gid in genre_ids)
    
    query = f"""
        g.V({formatted_ids})
         .hasLabel('Genre')
         .in('HAS_GENRE')
         .values('title')
         .dedup()
    """
    
    g = gremlin_client.Client("ws://localhost:8183/gremlin", "g")
    movies = g.submit(query).all().result()
    g.close()
    
    print(f"   PuppyGraph found these movies:")
    for m in movies:
        print(f"    - {m}")
    print("\nSUCCESS: LanceDB (semantics) + PuppyGraph (connections) working end-to-end!\n")

if __name__ == "__main__":
    main()
