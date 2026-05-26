import os, time, json, re
from flask import Flask, request, jsonify, send_from_directory

import psycopg2
import lancedb
from sentence_transformers import SentenceTransformer
from gremlin_python.driver import client as gremlin_client

LANCE_PATH   = "./lance_whales"
EMBED_MODEL  = "BAAI/bge-small-en-v1.5"
GREMLIN_URL  = "ws://localhost:8183/gremlin"
DB_PARAMS    = dict(host="localhost", port=5434, user="postgres", dbname="whale")

print("Loading embedding model...")
model = SentenceTransformer(EMBED_MODEL)
db    = lancedb.connect(LANCE_PATH)
tbl   = db.open_table("sightings")
print("Ready.")

app = Flask(__name__, static_folder="demo_ui")

def get_conn():
    return psycopg2.connect(**DB_PARAMS)

def clean_remarks(text):
    """Strip markdown links from remarks, keep the display text."""
    return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text or '')

# ─── Serve UI ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("demo_ui", "index.html")

# ─── Semantic search ──────────────────────────────────────────────────────────
@app.route("/query", methods=["POST"])
def query():
    q = request.json.get("query", "").strip()
    loc = request.json.get("location", "").strip()
    year = request.json.get("year", "").strip()
    k = request.json.get("k", 50) # Fetch more because filtering will drop results
    if not q:
        return jsonify({"error": "empty query"}), 400

    t0 = time.time()
    vec     = model.encode(q).tolist()
    results = tbl.search(vec).limit(k).to_pandas()
    sighting_ids = results["sighting_id"].tolist()
    t_lance = round((time.time() - t0) * 1000)

    if not sighting_ids:
        return jsonify({"query": q, "sightings": [], "ms_lance": t_lance})

    # Enrich with lat/lon/date/whale_name from DuckDB and apply filters
    conn = get_conn()
    cur  = conn.cursor()
    ids_str = ", ".join(f"'{sid}'" for sid in sighting_ids)
    
    loc_filter = f"AND s.locality ILIKE '%{loc}%'" if loc else ""
    year_filter = f"AND s.date LIKE '{year}%'" if year else ""
    
    cur.execute(f"""
        SELECT s.sighting_id, s.whale_id, w.whale_name, s.date, s.lat, s.lon, s.locality, s.observer, s.remarks
        FROM whale.public.sightings s
        JOIN whale.public.whales w ON s.whale_id = w.whale_id
        WHERE s.sighting_id IN ({ids_str})
        {loc_filter}
        {year_filter}
        ORDER BY s.date
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    conn.close()

    sightings = []
    for row in rows:
        d = dict(zip(cols, row))
        name = d["whale_name"]
        if not name or name == "(unknown)" or name == "(no name)": continue # Skip unnamed whales for the story UX
        sightings.append({
            "sighting_id": d["sighting_id"],
            "whale_id":    d["whale_id"],
            "whale_name":  name,
            "date":        str(d["date"]),
            "lat":         float(d["lat"]) if d["lat"] else None,
            "lon":         float(d["lon"]) if d["lon"] else None,
            "locality":    d["locality"],
            "observer":    d["observer"],
            "remarks":     clean_remarks(d["remarks"]),
        })

    return jsonify({"query": q, "sightings": sightings, "ms_lance": t_lance})

# ─── Whale journey: all sightings for one whale ──────────────────────────────
@app.route("/whale/<path:whale_id>/journey")
def whale_journey(whale_id):
    t0 = time.time()
    conn = get_conn()
    cur  = conn.cursor()

    # All sightings for this whale, chronological
    cur.execute("""
        SELECT s.sighting_id, w.whale_name, s.date, s.lat, s.lon, s.locality, s.observer, s.remarks
        FROM whale.public.sightings s
        JOIN whale.public.whales w ON s.whale_id = w.whale_id
        WHERE s.whale_id = %s
        ORDER BY s.date
    """, (whale_id,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]

    sightings = []
    whale_name = "Unknown"
    for row in rows:
        d = dict(zip(cols, row))
        whale_name = d["whale_name"]
        sightings.append({
            "sighting_id": d["sighting_id"],
            "date":        str(d["date"]),
            "lat":         float(d["lat"]) if d["lat"] else None,
            "lon":         float(d["lon"]) if d["lon"] else None,
            "locality":    d["locality"],
            "observer":    d["observer"],
            "remarks":     clean_remarks(d["remarks"]),
        })

    conn.close()
    ms = round((time.time() - t0) * 1000)
    return jsonify({"whale_id": whale_id, "whale_name": whale_name, "sightings": sightings, "ms": ms})

# ─── Network: connected whales + their journeys ──────────────────────────────
@app.route("/whale/<path:whale_id>/network")
def whale_network(whale_id):
    t0 = time.time()
    conn = get_conn()
    cur  = conn.cursor()

    # Find connected whales via shared_location (limit to top 8 by co-sighting count)
    cur.execute("""
        SELECT sl.whale_id_b, w.whale_name, sl.locality, sl.co_sightings
        FROM whale.public.shared_location sl
        JOIN whale.public.whales w ON sl.whale_id_b = w.whale_id
        WHERE sl.whale_id_a = %s
        AND w.whale_name IS NOT NULL AND w.whale_name != '(unknown)'
        ORDER BY sl.co_sightings DESC
        LIMIT 8
    """, (whale_id,))
    edges = cur.fetchall()

    connected = []
    for whale_id_b, whale_name, locality, co_sightings in edges:
        if whale_id_b == whale_id:
            continue

        # Get that whale's sightings
        cur.execute("""
            SELECT sighting_id, date, lat, lon, locality, observer, remarks
            FROM whale.public.sightings
            WHERE whale_id = %s
            ORDER BY date
        """, (whale_id_b,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        sightings = []
        for row in rows:
            d = dict(zip(cols, row))
            sightings.append({
                "sighting_id": d["sighting_id"],
                "date":        str(d["date"]),
                "lat":         float(d["lat"]) if d["lat"] else None,
                "lon":         float(d["lon"]) if d["lon"] else None,
                "locality":    d["locality"],
                "observer":    d["observer"],
                "remarks":     clean_remarks(d["remarks"]),
            })

        connected.append({
            "whale_id":      whale_id_b,
            "whale_name":    whale_name,
            "shared_locality": locality,
            "co_sightings":  co_sightings,
            "sightings":     sightings,
        })

    conn.close()
    ms = round((time.time() - t0) * 1000)
    return jsonify({
        "whale_id":  whale_id,
        "connected": connected,
        "ms_graph":  ms,
    })

# ─── Behavioral Network Analysis ──────────────────────────────────────────────
@app.route("/analyze_behavior", methods=["POST"])
def analyze_behavior():
    t0 = time.time()
    whale_ids = request.json.get("whale_ids", [])
    if not whale_ids:
        return jsonify({"error": "no whales provided"}), 400

    conn = get_conn()
    cur  = conn.cursor()
    
    # 1. Get all journeys for these whales to map them
    ids_str = ", ".join(f"'{wid}'" for wid in whale_ids)
    cur.execute(f"""
        SELECT s.whale_id, w.whale_name, s.lat, s.lon, s.date, s.locality
        FROM whale.public.sightings s
        JOIN whale.public.whales w ON s.whale_id = w.whale_id
        WHERE s.whale_id IN ({ids_str}) AND s.lat IS NOT NULL
        ORDER BY s.date
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    
    whales = {}
    for row in rows:
        d = dict(zip(cols, row))
        wid = d["whale_id"]
        if wid not in whales:
            whales[wid] = {"whale_id": wid, "whale_name": d["whale_name"], "sightings": []}
        whales[wid]["sightings"].append({
            "lat": float(d["lat"]), "lon": float(d["lon"]),
            "date": str(d["date"]), "locality": d["locality"]
        })

    # 2. Query PuppyGraph (DuckDB shared_location) to find social edges BETWEEN these specific whales
    cur.execute(f"""
        SELECT whale_id_a, whale_id_b, locality, co_sightings
        FROM whale.public.shared_location
        WHERE whale_id_a IN ({ids_str}) AND whale_id_b IN ({ids_str})
    """)
    edges = cur.fetchall()
    
    # Format edges
    social_links = []
    for ea, eb, loc, co in edges:
        social_links.append({"source": ea, "target": eb, "locality": loc, "weight": co})
        
    conn.close()
    ms = round((time.time() - t0) * 1000)
    
    return jsonify({
        "whales": list(whales.values()),
        "edges": social_links,
        "ms_graph": ms
    })

if __name__ == "__main__":
    os.makedirs("demo_ui", exist_ok=True)
    app.run(port=8051, debug=False)
