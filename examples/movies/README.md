# LanceDB + PuppyGraph: Movies Example

This project demonstrates how to combine **LanceDB** (for semantic vector search) with **PuppyGraph** (for high-performance graph traversal) to discover hidden connections in a dataset of movies.

## How It Works (No Fabrication)

This architecture does exactly what it says on the tin. There are no language models hallucinating results in the background.

1. **LanceDB Defines the Semantic Universe (`/search`)**
   When a user searches for "brooding psychological thriller," a local embedding model (`BAAI/bge-small-en-v1.5`) converts that text into a vector. LanceDB queries its vector index to find the 18 movies whose plot descriptions mathematically align closest to that vector. 

2. **PuppyGraph Traverses the Creator Network (`/analyze`)**
   We take those exact 18 `movie_ids` and pass them to PuppyGraph via a Gremlin query. PuppyGraph traverses outwards along the `FEATURES` (actors) and `PRODUCED_BY` (producers) edges. It runs a `groupCount()` to see which creators appear most frequently inside this 18-movie cluster. We filter these creators by "Signal Strength" - they are only considered relevant if a high percentage of their *entire catalog* exists within this specific semantic cluster.

3. **The Graph Discovery Pipeline (`/discover`)**
   Once PuppyGraph identifies the top "Shared Creators", it hops *back* down to all the other films those creators have made. These are films that share creative DNA with the semantic cluster, but were completely missed by the initial vector search. 
   
   To ensure quality, those newly discovered graph-only films are pulled back into Python, where LanceDB re-scores their plot embeddings against the original user query. The films that score highest are surfaced as "Graph Discoveries."

Everything you see in the UI is backed by raw graph math and vector distances.

## Running the Demo

1. Start PuppyGraph locally (ensure it is configured to read from `movies.db` via the Postgres wire protocol on port 5435).
2. Install the Python dependencies in the virtual environment.
3. Run the FastAPI server:
   ```bash
   python demo_server.py
   ```
4. Open `http://localhost:8052` to use the Graph Discovery Engine.
