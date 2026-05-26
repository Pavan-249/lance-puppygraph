# Whale Demo Handoff

## What is done

- `load_data.py` ran successfully: 2605 sightings, 1253 whales, 289874 shared-location edges
- DuckDB: `whale.db` (tables: public.whales, public.sightings, public.shared_location)
- LanceDB: `lance_whales/` (table: sightings, embedded remarks text)
- `config.yaml` written (proxy port 5434, catalog=whale)
- `schema.json` written (Whale + Sighting vertices, SPOTTED_AT + SHARES_LOCATION edges)
- FusionProxy running: `python ../../fusion_proxy.py --config config.yaml` (PID in background, port 5434)

## What still needs doing

### 1. Upload schema to PuppyGraph UI

Docker maps 8085 -> 8081 (internal).
Go to http://localhost:8085, log in (puppygraph / puppygraph123).
Navigate to Schema tab, click Upload Schema, upload `examples/whales/schema.json`.

The FusionProxy must be running first (it is, on port 5434). PuppyGraph will validate
the JDBC connection on schema upload.

Gremlin endpoint: ws://localhost:8183/gremlin

### 2. Build demo.py (LanceDB + PuppyGraph query)

```python
import lancedb
from sentence_transformers import SentenceTransformer
from gremlin_python.driver import client as gremlin_client

LANCE_PATH = "./lance_whales"
model = SentenceTransformer("BAAI/bge-small-en-v1.5")
db = lancedb.connect(LANCE_PATH)
tbl = db.open_table("sightings")

query = "feeding near glaciers"
vec = model.encode(query).tolist()
hits = tbl.search(vec).limit(5).to_pandas()

# hits has: sighting_id, whale_id, remarks
# Use whale_ids to find connected whales via PuppyGraph

whale_ids = [f"'Whale[{w}]'" for w in hits["whale_id"].unique()]
ids_str = ", ".join(whale_ids)

g = gremlin_client.Client("ws://localhost:8183/gremlin", "g")

# Find whales that share feeding locations with our matched whales
connected = g.submit(f"""
    g.V({ids_str}).hasLabel('Whale')
     .out('SHARES_LOCATION')
     .dedup()
     .project('name','id','locality')
       .by('whale_name')
       .by('whale_id')
       .by(__.in('SPOTTED_AT').values('locality').dedup().fold())
""").all().result()

for w in connected:
    print(w)

g.close()
```

### 3. Build Flask demo server + UI

Pattern is identical to `demo_server.py` in the repo root but for whales:
- POST /query with { "query": "..." }
- Step 1: LanceDB search on sighting remarks -> matched sighting rows (real observer text)
- Step 2: Gremlin SHARES_LOCATION traversal -> connected whales + their localities
- Return JSON: { matched_sightings, connected_whales, ms_lance, ms_graph }

UI should show:
- Search box with example queries ("feeding near ice", "traveling with calf", "breaching behavior")
- Pipeline trace: LanceDB ms | PuppyGraph ms
- Left panel: matched sightings with real remarks text (no fabrication)
- Right panel: cross-whale network (whales that share feeding grounds with matched whales)

### 4. Fix whale-mockup/index.html (separate task)

- Replace fabricated descriptions with real GBIF occurrence_remarks
- Remove all em-dashes
- Improve text readability (contrast on dark overlay)
- Multiple whale support

## Key files

```
examples/whales/
  load_data.py      done
  config.yaml       done
  schema.json       done
  whale.db          built
  lance_whales/     built
  HANDOFF.md        this file
```

## Running the proxy

```bash
cd /Users/pavankumar_s/Desktop/lance-puppygraph/examples/whales
source ../../venv/bin/activate
python ../../fusion_proxy.py --config config.yaml
```

## Graph schema summary

- Whale vertex: whale_id (PK), whale_name, happywhale_url
- Sighting vertex: sighting_id (PK), whale_id, date, lat, lon, locality, observer, remarks
- SPOTTED_AT edge: Whale -> Sighting (from sightings.whale_id -> sightings.sighting_id)
- SHARES_LOCATION edge: Whale -> Whale (from shared_location.whale_id_a -> whale_id_b)
  - Two whales are connected if they were spotted in the same locality
  - 289874 such pairs in the data
