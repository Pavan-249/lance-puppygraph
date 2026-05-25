"""
Beauty Products Example — Demo Query
=====================================

End-to-end demo: takes a skin symptom in plain English, finds semantically
similar ingredients via LanceDB, then traverses the PuppyGraph graph to
find products containing those ingredients.

Prerequisites:
    1. Run `python load_data.py` to create data
    2. Start the proxy: `python ../../fusion_proxy.py --config config.yaml`
    3. Start PuppyGraph: `docker compose -f ../../docker-compose.yml up -d`
    4. Upload schema.json to PuppyGraph UI at http://localhost:8085

Usage:
    python demo.py
    python demo.py --symptom "my skin feels dry and flaky"
"""

import argparse

import lancedb
from gremlin_python.driver import client as gremlin_client
from sentence_transformers import SentenceTransformer

PUPPYGRAPH_URL = "ws://localhost:8183/gremlin"
LANCE_PATH = "./lance_data"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

DEFAULT_SYMPTOM = (
    "skin gets tight and red after moisturiser, "
    "burning sensation, worse in dry weather"
)


def main():
    parser = argparse.ArgumentParser(description="Beauty graph demo query")
    parser.add_argument(
        "--symptom", default=DEFAULT_SYMPTOM,
        help="Skin symptom to search for (natural language)",
    )
    parser.add_argument("--k", type=int, default=10, help="Number of similar ingredients")
    args = parser.parse_args()

    model = SentenceTransformer(EMBED_MODEL)
    lance_db = lancedb.connect(LANCE_PATH)

    print(f"Symptom query: '{args.symptom}'\n")

    # Step 1: Vector search for similar ingredients
    query_vec = model.encode(args.symptom).tolist()
    lance_table = lance_db.open_table("ingredients")
    similar = lance_table.search(query_vec).limit(args.k).to_pandas()

    print("Step 1: LanceDB found these semantically similar ingredients:")
    for _, row in similar.iterrows():
        print(f"  {row['ingredient_name']}")

    # Step 2: Graph traversal to find products
    seed_ids = similar["ingredient_id"].tolist()
    puppygraph_ids = ", ".join(f"'Ingredient[{i}]'" for i in seed_ids)

    print(f"\nStep 2: PuppyGraph traversal from {len(seed_ids)} seed ingredients...")

    g = gremlin_client.Client(PUPPYGRAPH_URL, "g")
    query = f"""
        g.V({puppygraph_ids})
         .in('CONTAINS')
         .values('product_name')
         .groupCount()
         .order(local).by(values, desc)
         .limit(local, 10)
    """
    results = g.submit(query).all().result()
    g.close()

    print("\nProducts with the most flagged ingredients:")
    for product_name, count in results[0].items():
        print(f"  {product_name} — {count} flagged ingredients")

    print("\nWhat this combination enables:")
    print("  LanceDB alone:  gives you ingredients, not products")
    print("  PuppyGraph alone: needs exact ingredient IDs, not free text")
    print("  Together: plain English symptom → ingredient cluster → product risk graph")


if __name__ == "__main__":
    main()
