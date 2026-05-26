# Movies Example — Setup Guide

## What this demonstrates

**The genuine value of LanceDB + PuppyGraph together:**

| Tool alone | What it finds |
|---|---|
| LanceDB only | Films whose *descriptions* match your vibe |
| PuppyGraph only | Connected films, but needs an exact starting ID |
| Together | Films whose *descriptions* match **+ films the same creators made** that don't sound similar but were validated by the same human taste |

The "Graph Discovery" tab in the UI shows this contrast explicitly: films invisible to semantic search, surfaced through producer/actor graph traversal, then re-ranked by LanceDB to show how close they actually are to your vibe.

---

## Architecture

```
Browser
  │
  ▼
demo_server.py  (Flask, port 8052)
  │
  ├─── /search       LanceDB Python client  ──▶  lance_movies/
  │
  ├─── /analyze      psycopg2  ──▶  FusionProxy (:5435)  ──▶  movies.db (DuckDB)
  ├─── /analyze_gems      │
  └─── /discover          │
                          │
                    PuppyGraph (Docker)
                    also connects here via JDBC
                    for Gremlin queries against
                    the same DuckDB tables
```

> **Note on Gremlin:** The web server uses direct SQL to the FusionProxy instead of Gremlin, because the `gremlin-python` WebSocket client deadlocks on macOS when called from a Flask thread. PuppyGraph and the FusionProxy are both running and the schema is validated — the CLI demo (`python demo.py`) uses Gremlin properly.

---

## Prerequisites

- Python venv with `requirements.txt` installed
- Docker (for PuppyGraph)
- `movies.db` and `lance_movies/` built by `load_data.py`

---

## Step 1 — Start PuppyGraph

From the repo root:

```bash
docker compose up -d
```

Wait ~30 seconds for PuppyGraph to finish starting. Verify at http://localhost:8085 (login: `puppygraph` / `puppygraph123`).

---

## Step 2 — Start the FusionProxy

From the repo root:

```bash
source venv/bin/activate
python fusion_proxy.py --config examples/movies/config.yaml
```

The proxy listens on **port 5435**. It bridges PuppyGraph and the `movies.db` DuckDB file, and intercepts `vec_search(table, 'query', k)` calls to run them against LanceDB.

---

## Step 3 — Upload the Schema to PuppyGraph

This registers the graph structure (Movie, Actor, Producer vertices + FEATURES, PRODUCED_BY edges) with PuppyGraph.

```bash
curl -XPOST -H "content-type: application/json" \
     --data-binary @examples/movies/schema.json \
     --user "puppygraph:puppygraph123" \
     localhost:8085/schema
```

Expected response:
```json
{"Status":"OK","Message":"Schema updated successfully","Updated":true}
```

Or if already loaded: `"No schema changes detected"` — that's also fine.

You can also do this via the PuppyGraph UI:
1. Open http://localhost:8085
2. Log in with `puppygraph` / `puppygraph123`
3. Go to **Schema** → **Upload Schema** → select `examples/movies/schema.json`

---

## Step 4 — Start the Demo Server

```bash
cd examples/movies
python demo_server.py
```

Open **http://localhost:8052**.

---

## Step 5 — Try the Demo

In the UI, search for a vibe like:

- `"heist with a clever twist ending"`
- `"epic fantasy quest with magic"`
- `"brooding psychological thriller with isolation"`
- `"dark AI rebellion and dystopia"`

Then click **"Graph Discovery"** in the right panel to see films the graph found that semantic search missed.

---

## Schema Reference

The graph schema (`schema.json`) defines:

| Vertex | Table | ID field |
|---|---|---|
| `Movie` | `public.movies` | `movie_id` |
| `Actor` | `public.actors` | `actor_id` |
| `Producer` | `public.producers` | `producer_id` |

| Edge | Table | Direction |
|---|---|---|
| `FEATURES` | `public.acts_in` | Movie → Actor |
| `PRODUCED_BY` | `public.produced_by` | Movie → Producer |

PuppyGraph connects via JDBC to `jdbc:postgresql://host.docker.internal:5435/movies` (the FusionProxy).

---

## Troubleshooting

**Port 5435 not listening:**
The proxy exited. Re-run `python fusion_proxy.py --config examples/movies/config.yaml`. Check that `examples/movies/movies.db` exists.

**Schema upload returns 404:**
Make sure you're posting to port `8085` (not `8081`), path `/schema` (not `/api/schema`).

**`/discover` returns empty graph_finds:**
The cluster may be too small, or the producers in that cluster made very few films total. Try a broader query (more common genre) or increase the search `k` parameter.

**First search is slow (~3-4 seconds):**
The embedding model loads lazily on first request. Subsequent searches are fast (<200ms).
