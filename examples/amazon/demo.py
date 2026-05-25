import argparse
import lancedb
import pandas as pd
from pathlib import Path
from gremlin_python.driver import client as gremlin_client
from sentence_transformers import SentenceTransformer

LANCE_PATH     = str(Path(__file__).parent / "lance_amazon")
EMBED_MODEL    = "BAAI/bge-small-en-v1.5"
PUPPYGRAPH_URL = "ws://localhost:8183/gremlin"
TOP_K          = 200


def gremlin(g, query):
    return g.submit(query).all().result()


def extract_id(vertex_str):
    s = str(vertex_str)
    if "[" in s:
        return s.split("[")[-1].rstrip("]").rstrip("]")
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="this product literally changed my life I was in tears my whole family is amazed")
    args = parser.parse_args()

    print(f"\nQuery: \"{args.query}\"\n")
    print("=" * 70)

    model = SentenceTransformer(EMBED_MODEL)
    db    = lancedb.connect(LANCE_PATH)
    tbl   = db.open_table("reviews")

    matches = tbl.search(model.encode(args.query).tolist()).limit(TOP_K).to_pandas()

    print(f"\nSTEP 1  LanceDB semantic search")
    print(f"  Found {len(matches)} reviews matching that emotional vibe\n")
    for _, row in matches.head(5).iterrows():
        rating = str(row["rating"]).replace(".0", "")
        print(f"  {rating}/5  \"{str(row['text'])[:90]}\"")

    suspicious_users = matches["user_id"].unique().tolist()
    print(f"\n  {len(suspicious_users)} unique accounts wrote reviews with this vibe")

    # build a lookup: product_id -> sample review text from the matched set
    product_samples = (
        matches.groupby("product_id")["text"]
        .first()
        .str[:80]
        .to_dict()
    )

    g   = gremlin_client.Client(PUPPYGRAPH_URL, "g")
    ids = ", ".join(f"'Reviewer[{uid}]'" for uid in suspicious_users[:100])

    print(f"\n{'=' * 70}")
    print(f"\nSTEP 2  PuppyGraph graph traversal")
    print(f"  Mapping what those {min(len(suspicious_users), 100)} accounts reviewed together...\n")

    product_counts = gremlin(g, f"""
        g.V({ids}).out('WROTE_REVIEW').groupCount().order(local).by(values, desc).limit(local, 10)
    """)

    print("  Products reviewed by multiple flagged accounts:")
    if product_counts:
        for vertex, count in product_counts[0].items():
            pid     = extract_id(vertex)
            sample  = product_samples.get(pid, "")
            snippet = f"  \"{sample}...\"" if sample else ""
            label   = f"{count} flagged accounts reviewed this"
            print(f"    {label}")
            if snippet:
                print(f"    {snippet}")
            print()
    else:
        print("    (no overlap found)")

    co_reviewers = gremlin(g, f"""
        g.V({ids}).out('WROTE_REVIEW').in('WROTE_REVIEW').groupCount().order(local).by(values, desc).limit(local, 20)
    """)

    print("  Accounts most deeply entangled with the flagged cluster:")
    cartel_size = 0
    if co_reviewers:
        for reviewer_v, count in co_reviewers[0].items():
            if count >= 3:
                cartel_size += 1
                uid = extract_id(reviewer_v)
                # find a sample review from this reviewer in our matched set
                reviewer_reviews = matches[matches["user_id"] == uid]["text"]
                if not reviewer_reviews.empty:
                    snippet = f"  wrote: \"{str(reviewer_reviews.iloc[0])[:70]}...\""
                else:
                    snippet = ""
                print(f"    {uid[:30]}  shared {count} products with flagged group")
                if snippet:
                    print(f"    {snippet}")
                print()
    else:
        print("    (no results)")

    g.close()

    print(f"{'=' * 70}")
    print(f"\nVerdict")
    print(f"  {cartel_size} accounts share 3+ reviewed products with the flagged cluster")
    print(f"  A normal reviewer overlaps with strangers on 0-1 products by chance")
    print(f"  Sharing 10, 50, 190 products means coordinated activity, not coincidence")
    print()
    print(f"  LanceDB alone:    finds suspicious reviews, cannot prove coordination")
    print(f"  PuppyGraph alone: traces connections, needs exact IDs to start from")
    print(f"  Together:         vibe search -> suspects -> graph proof of the ring")


if __name__ == "__main__":
    main()
