# LanceDB + PuppyGraph Connector

This repository contains a connector (`fusion_proxy.py`) that seamlessly integrates **PuppyGraph** (for graph traversal) with **LanceDB** (for semantic vector search).

PuppyGraph natively connects to relational databases via JDBC. This project provides a proxy that acts as a PostgreSQL server, allowing PuppyGraph to traverse relationships stored in DuckDB while routing semantic search queries directly to LanceDB.

## Architecture

1.  **PuppyGraph**: Executes graph traversals (Gremlin/Cypher). Connects to the proxy via JDBC.
2.  **FusionProxy**: Intercepts queries. Standard SQL queries are passed to DuckDB. Calls to the custom `vec_search()` macro are intercepted and executed via the LanceDB Python SDK.
3.  **LanceDB**: Handles all vector similarity searches.
4.  **DuckDB**: Stores the relational graph data (nodes and edges).

## How it Works

The connector uses `buenavista` to emulate a PostgreSQL wire protocol server. When PuppyGraph issues a query, the proxy parses it:

*   If the query contains `vec_search(table, 'query_string', k)`, the proxy strips this out, queries LanceDB for the top `k` semantic matches, and injects the resulting IDs back into the SQL query as an `IN (...)` clause.
*   The modified SQL query is then executed on the DuckDB instance.
*   The results are returned to PuppyGraph as if they came from a standard Postgres table.

This allows you to filter graph nodes using LanceDB's semantic search *before* executing complex graph traversals in PuppyGraph.

## Quick Start (Movies Demo)

The `examples/movies/` directory contains a full end-to-end demonstration. It uses a movie dataset to show how LanceDB can find movies by plot description, and PuppyGraph can traverse the shared creative network (directors and actors) to find hidden connections.

### 1. Start PuppyGraph
From the root directory, start the PuppyGraph Docker container:
```bash
docker compose up -d
```
PuppyGraph will be available at `http://localhost:8085` (Login: `puppygraph` / `puppygraph123`).

### 2. Start the Proxy
The proxy requires the path to the DuckDB file and the LanceDB directory. A configuration file is provided in the examples folder.

```bash
python fusion_proxy.py --config examples/movies/config.yaml
```
The proxy will listen on port `5435`.

### 3. Upload the Schema
Register the graph structure with PuppyGraph.

```bash
curl -XPOST -H "content-type: application/json" \
     --data-binary @examples/movies/schema.json \
     --user "puppygraph:puppygraph123" \
     localhost:8085/schema
```

### 4. Run the Demo Server
Start the frontend interface.

```bash
cd examples/movies
python demo_server.py
```
Open `http://localhost:8052` in your browser.

*(Insert UI Screenshots Here)*
