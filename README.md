# lance-puppygraph

A PostgreSQL-wire-protocol proxy that bridges [PuppyGraph](https://puppygraph.com) (graph query engine) with [LanceDB](https://lancedb.github.io/lancedb/) (vector database) using [DuckDB](https://duckdb.org) as the relational backbone.

PuppyGraph connects to the proxy via JDBC, treating it as a standard PostgreSQL server. The proxy intercepts a virtual `vec_search(table, query, k)` function in any SQL it receives, runs the corresponding vector similarity search in LanceDB, and substitutes the result IDs back into the query before passing it to DuckDB. Everything else passes through unchanged.

The result: Gremlin queries against PuppyGraph can start from a natural-language description instead of an exact entity ID. No custom PuppyGraph plugins, no external services, no changes to PuppyGraph configuration beyond the JDBC URI.

## Architecture

```
You (Gremlin / SQL client)
  |
  v
PuppyGraph  (translates Gremlin to SQL, sends via JDBC)
  |
  v
FusionProxy  (PostgreSQL wire-protocol server on port 5433)
  |
  +-- regular SQL  -----------> DuckDB  (graph tables in a .db file)
  |
  +-- vec_search() in SQL ----> LanceDB (vector similarity search)
                                   |
                                   `-- result IDs injected back into SQL -> DuckDB
  |
  v
PuppyGraph receives SQL results, returns Gremlin results to you
```

The proxy is a single file (`fusion_proxy.py`) that applies three patches at the SQL interception layer:

1. **JDBC probe silencing.** PuppyGraph sends `SHOW TRANSACTION ISOLATION LEVEL` and various `SET` statements on connect. DuckDB does not support these. The proxy swallows them and returns dummy results.

2. **pg_catalog join patch.** PuppyGraph introspects column types using `INNER JOIN pg_catalog.pg_type`. DuckDB's `pg_catalog` is incomplete and some type OIDs are absent, which causes the join to drop rows. The proxy rewrites these to `LEFT JOIN`.

3. **vec_search() interception.** Any SQL string (or parameterized query value) containing `vec_search(table, 'query text', k)` is caught. The proxy embeds the query text at runtime using the configured SentenceTransformers model, calls `lance_vector_search()` via the DuckDB `lance` extension, and splices the result IDs back as an `IN (...)` subquery.

## Requirements

- Python 3.9 or later
- Docker (for PuppyGraph)
- 4 GB RAM minimum (embedding models load into memory)
- Internet access on first run (model download from HuggingFace, ~130 MB)

## Install

```bash
git clone https://github.com/Pavan-249/lance-puppygraph.git
cd lance-puppygraph
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

For the examples you also need:

```bash
pip install datasets pandas gremlinpython
```

## Quick Start

This uses a small built-in dataset (10 movies, 8 genres) so you can verify the full stack without downloading anything large.

### Step 1: Create the toy data

```bash
python create_test_data.py
```

Creates `test_data.db` (DuckDB) and `./test_lance_data/` (LanceDB) with genre embeddings. Takes about 30 seconds.

### Step 2: Start PuppyGraph

```bash
docker compose up -d
```

Wait about 30 seconds for PuppyGraph to finish starting. The UI will be available at http://localhost:8085.

### Step 3: Start the proxy

```bash
python fusion_proxy.py
```

The default `config.yaml` already points at the toy dataset. The proxy listens on port 5433.

### Step 4: Upload the schema to PuppyGraph

Open http://localhost:8085 and log in with `puppygraph` / `puppygraph123`.

Go to **Schema**, click **Upload Schema**, and select `test_schema.json` from the repo root. PuppyGraph will connect to the proxy as part of schema validation.

### Step 5: Run the demo

```bash
python demo.py --vibe "dark scary nightmares"
python demo.py --vibe "space exploration adventure"
python demo.py --vibe "love and heartbreak"
```

LanceDB finds the closest genre embeddings. PuppyGraph traverses the graph to return movies tagged with those genres. You should see different films for each vibe.

---

## Examples

Each example under `examples/` follows the same pattern:

```
cd examples/<name>
python load_data.py                                  # download and build data
python ../../fusion_proxy.py --config config.yaml    # start the proxy
# upload schema.json in the PuppyGraph UI
python demo.py [--args]
```

### IMDB movies (`examples/imdb/`)

**What it demonstrates:** Find films by plot concept, then explore the graph to surface shared actors, directors, and genres across the matched set.

**Data source:** TMDB 5000 Movies via HuggingFace (`AiresPucrs/tmdb-5000-movies`)

**Graph:**
- `Movie` vertices (with LanceDB embeddings of plot overviews)
- `Director`, `Actor`, `Genre` vertices
- `DIRECTED_BY`, `FEATURES`, `HAS_GENRE` edges

```bash
cd examples/imdb
python load_data.py     # 5 to 10 min
python ../../fusion_proxy.py --config config.yaml
# upload schema.json to PuppyGraph UI
python demo.py --query "a man who discovers he has been living in a simulated reality"
```

The demo returns semantically matched films, then traverses the graph to show actors and directors shared across those films, and recommends other movies through shared cast.

### Amazon reviews (`examples/amazon/`)

**What it demonstrates:** Detect coordinated fake reviews by finding accounts whose reviews share a suspicious writing style, then checking how many products those accounts reviewed in common.

**Data source:** Amazon Reviews 2023 via HuggingFace (`gmongaras/Amazon-Reviews-2023`), first 40,000 reviews streamed

**Graph:**
- `Reviewer` vertices
- `Product` vertices
- `WROTE_REVIEW` edges

```bash
cd examples/amazon
python load_data.py     # 5 to 15 min
python ../../fusion_proxy.py --config config.yaml
# upload schema.json to PuppyGraph UI
python demo.py --query "this product literally changed my life I was in tears"
```

LanceDB finds reviews matching the emotional pattern. PuppyGraph traverses `WROTE_REVIEW` edges to identify whether those reviewers cluster around the same products, which indicates coordinated activity.

---

## Configuration Reference

Configuration is in a YAML file (default `./config.yaml`). Pass a different path with `--config path/to/file.yaml`. Relative paths inside the config are resolved relative to the config file's directory, not the working directory.

| Key | Default | Description |
|---|---|---|
| `proxy.host` | `0.0.0.0` | Interface the proxy binds to |
| `proxy.port` | `5433` | Port the proxy listens on |
| `duckdb.path` | `data.db` | Path to the DuckDB file |
| `lancedb.path` | `./lance_data` | Path to the LanceDB directory |
| `embedding.model` | `BAAI/bge-small-en-v1.5` | SentenceTransformers model for query embedding at runtime. Must match the model used when creating the LanceDB embeddings. |
| `catalog.name` | `graph_data` | DuckDB catalog name. The PuppyGraph JDBC URI must use this as the database name. |
| `vector_search.id_column` | `id` | Column in the LanceDB table that holds the entity ID. This is the value substituted into the `IN (...)` clause. |
| `vector_search.vector_column` | `vector` | Column in the LanceDB table that holds the embedding. |

Example:

```yaml
proxy:
  host: "0.0.0.0"
  port: 5433

duckdb:
  path: "my_data.db"

lancedb:
  path: "./lance_data"

embedding:
  model: "BAAI/bge-small-en-v1.5"

catalog:
  name: "my_graph"

vector_search:
  id_column: "entity_id"
  vector_column: "vector"
```

---

## vec_search() Function

Use this virtual function anywhere in a SQL query that passes through the proxy:

```sql
vec_search(table_name, 'natural language query', k)
```

Parameters:

- `table_name`: the LanceDB table to search (string, no quotes around the identifier)
- `'natural language query'`: the search query, embedded at runtime using the configured model
- `k`: number of nearest neighbors to return (integer)

The proxy detects `vec_search()` in two positions:

**Inline in SQL:**
```sql
SELECT * FROM products
WHERE ingredient_id IN (vec_search(ingredients, 'irritating to sensitive skin', 10))
```

**In a parameterized query parameter value** (PuppyGraph sometimes passes subqueries as parameter values rather than inline):
```sql
SELECT * FROM products WHERE ingredient_id = ?
-- with parameter: vec_search(ingredients, 'irritating to sensitive skin', 10)
```

Both forms are intercepted and rewritten to:
```sql
... IN (SELECT entity_id FROM lance_vector_search('lance_ns.main.table_name', 'vector', [...], k=10))
```

---

## PuppyGraph Schema Format

PuppyGraph requires a JSON schema that describes vertices, edges, and the JDBC connection. The JDBC URI must point at the proxy:

```
jdbc:postgresql://host.docker.internal:5433/{catalog_name}?ssl=false&sslmode=disable
```

Use `host.docker.internal` so that PuppyGraph running inside Docker can reach the proxy on your host machine. On Linux this alias is not available by default (see Troubleshooting).

Credentials are `postgres` / `postgres` (BuenaVista wire-protocol defaults, not real database credentials).

Minimal schema structure:

```json
{
  "catalogs": [{
    "name": "my_graph",
    "type": "postgresql",
    "jdbc": {
      "username": "postgres",
      "password": "postgres",
      "jdbcUri": "jdbc:postgresql://host.docker.internal:5433/my_graph?ssl=false&sslmode=disable",
      "driverClass": "org.postgresql.Driver"
    }
  }],
  "graph": {
    "vertices": [{
      "label": "MyEntity",
      "oneToOne": {
        "tableSource": {
          "catalog": "my_graph",
          "schema": "public",
          "table": "entities"
        },
        "id": {
          "fields": [{ "type": "String", "field": "entity_id", "alias": "entity_id" }]
        },
        "attributes": [
          { "type": "String", "field": "name", "alias": "name" }
        ]
      }
    }],
    "edges": [{
      "label": "RELATED_TO",
      "fromVertex": "MyEntity",
      "toVertex": "MyEntity",
      "tableSource": {
        "catalog": "my_graph",
        "schema": "public",
        "table": "entity_relations"
      },
      "fromId": { "fields": [{ "type": "String", "field": "from_id", "alias": "from_id" }] },
      "toId":   { "fields": [{ "type": "String", "field": "to_id",   "alias": "to_id" }] }
    }]
  }
}
```

See the `schema.json` files in each `examples/` subdirectory for complete working schemas.

---

## Building Your Own Use Case

**Step 1: Create a DuckDB file with your graph tables**

Tables must be in the `public` schema so PuppyGraph's queries resolve correctly.

```python
import duckdb

con = duckdb.connect("my_data.db")
con.execute("CREATE SCHEMA IF NOT EXISTS public")
con.execute("""
    CREATE TABLE public.entities (
        entity_id VARCHAR,
        name      VARCHAR
    )
""")
con.execute("""
    CREATE TABLE public.relations (
        from_id VARCHAR,
        to_id   VARCHAR
    )
""")
# insert your data
con.close()
```

**Step 2: Embed your entities into LanceDB**

The table must have the ID column (matching `vector_search.id_column` in config) and the vector column (matching `vector_search.vector_column`).

```python
import lancedb
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("BAAI/bge-small-en-v1.5")

data = []
for entity_id, description in my_entities:
    data.append({
        "entity_id": entity_id,
        "description": description,
        "vector": model.encode(description).tolist(),
    })

db = lancedb.connect("./lance_data")
db.create_table("entities", data=data, mode="overwrite")
```

**Step 3: Write config.yaml**

Match `catalog.name` to the database name in your PuppyGraph JDBC URI. Match `id_column` and `vector_column` to the column names in your LanceDB table.

**Step 4: Write schema.json for PuppyGraph**

Define vertices and edges pointing at your tables in the `public` schema. Use the catalog name from step 3.

**Step 5: Start everything**

```bash
docker compose up -d
python fusion_proxy.py --config config.yaml
# open http://localhost:8085, upload schema.json
```

---

## Project Structure

```
lance-puppygraph/
├── fusion_proxy.py          # Proxy server (config-driven, no example-specific code)
├── config.yaml              # Default config (points at toy dataset)
├── create_test_data.py      # Builds the toy dataset
├── test_schema.json         # PuppyGraph schema for the toy dataset
├── demo.py                  # Demo query for the toy dataset
├── docker-compose.yml       # PuppyGraph Docker service definition
├── requirements.txt         # Core Python dependencies
└── examples/
    ├── imdb/
    │   ├── load_data.py     # Downloads TMDB 5000 and builds data files
    │   ├── config.yaml
    │   ├── schema.json
    │   └── demo.py
    └── amazon/
        ├── load_data.py     # Streams Amazon Reviews and builds data files
        ├── config.yaml
        ├── schema.json
        └── demo.py
```

---

## Troubleshooting

**PuppyGraph cannot connect to the proxy**

On Linux, `host.docker.internal` is not available by default. Either add `--add-host=host.docker.internal:host-gateway` to the Docker run command, or replace `host.docker.internal` in your `schema.json` with your machine's LAN IP address.

**"DuckDB file not found" or "LanceDB path not found" at startup**

Paths in `config.yaml` resolve relative to the config file's location, not your working directory. If you run `python fusion_proxy.py --config examples/imdb/config.yaml` from the repo root, the path `imdb.db` inside that config resolves to `examples/imdb/imdb.db`.

**Schema upload fails in the PuppyGraph UI**

PuppyGraph validates the JDBC connection when you upload the schema. Make sure the proxy is running first.

**Embedding model takes a long time on first run**

SentenceTransformers downloads the model from HuggingFace on first use and caches it in `~/.cache/torch/sentence_transformers/`. The `BAAI/bge-small-en-v1.5` model is about 130 MB. Subsequent runs use the local cache.

**vec_search() returns no results or wrong results**

- Confirm that `vector_search.id_column` and `vector_search.vector_column` in config match the exact column names in your LanceDB table. Column names are case-sensitive.
- Confirm that the `embedding.model` in config matches the model used when creating the LanceDB embeddings. Using different models for indexing and querying produces meaningless distances.

**JDBC 4-byte ping errors in proxy log**

These are expected. PuppyGraph sends 4-byte probe packets on connection establishment that do not conform to the PostgreSQL protocol. The proxy catches and silences these in `QuietBuenaVistaServer.handle_error()`. They do not affect functionality.

**vec_search() not being intercepted**

Check that the function call appears exactly as `vec_search(table, 'query', k)` with no extra whitespace inside the parentheses around the table name. The regex allows whitespace around commas but expects the identifier to be a plain word (no quotes).

---

## Dependencies

**Core (required for the proxy):**

| Package | Purpose |
|---|---|
| `buenavista` | PostgreSQL wire-protocol server |
| `duckdb` | Relational engine with `lance` extension for native vector search |
| `lancedb` | Vector database |
| `sentence-transformers` | Text embedding at query time |
| `pyarrow` | Columnar data interchange |
| `pyyaml` | Config file parsing |

**Examples (optional):**

| Package | Purpose |
|---|---|
| `datasets` | HuggingFace dataset loading in `load_data.py` scripts |
| `pandas` | Data manipulation in `load_data.py` scripts |
| `gremlinpython` | Gremlin client for the demo scripts |
