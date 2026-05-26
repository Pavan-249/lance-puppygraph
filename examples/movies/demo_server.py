"""
Movies demo.FastAPI + PuppyGraph (Gremlin) + LanceDB

Architecture:
  LanceDB (Python)     → semantic search on movie plot embeddings
  PuppyGraph (Gremlin)  → graph traversal: Movie→Actor, Movie→Producer edges
  FusionProxy (:5435)   → Postgres-wire bridge; PuppyGraph connects here via JDBC

Endpoints:
  /search    LanceDB semantic search
  /analyze   Gremlin: find top actors/producers in cluster, chemistry pairs
  /discover  Gremlin hop to other films by cluster producers, LanceDB re-rank
"""
import os, time, traceback
from collections import defaultdict
from itertools import combinations
import numpy as np

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List

import lancedb
from sentence_transformers import SentenceTransformer
from gremlin_python.driver import client as gremlin_client

LANCE_PATH     = "./lance_movies"
EMBED_MODEL    = "BAAI/bge-small-en-v1.5"
PUPPYGRAPH_URL = "ws://localhost:8183/gremlin"

_model = None
_tbl   = None

def get_model():
    global _model
    if _model is None:
        print("Loading embedding model...")
        _model = SentenceTransformer(EMBED_MODEL)
    return _model

def get_tbl():
    global _tbl
    if _tbl is None:
        _tbl = lancedb.connect(LANCE_PATH).open_table("movies")
    return _tbl

def new_gremlin():
    """Open a Gremlin WebSocket session to PuppyGraph."""
    return gremlin_client.Client(PUPPYGRAPH_URL, "g")

def gquery(g, q):
    """Submit a Gremlin query string and return the result list."""
    return g.submit(q).all().result()

def extract_id(v):
    """Extract raw ID from PuppyGraph vertex ref like v[Actor[abc123]]."""
    s = str(v)
    while "[" in s:
        s = s.split("[", 1)[-1]
        if s.endswith("]"):
            s = s[:-1]
    return s


app = FastAPI()

@app.get("/")
async def index():
    return FileResponse("demo_ui/index.html")


# ── /search ──────────────────────────────────────────────────────────────────
class SearchReq(BaseModel):
    query: str
    k: int = 150

@app.post("/search")
def search(req: SearchReq):
    q = req.query.strip()
    if not q:
        return {"error": "empty query"}
    t0  = time.time()
    vec = get_model().encode(q).tolist()
    res = get_tbl().search(vec).limit(req.k).to_pandas()
    ms  = round((time.time() - t0) * 1000)
    movies = [{
        "movie_id": r["movie_id"],
        "title":    r["title"],
        "overview": r["overview"],
        "genres":   r["genres"],
        "distance": float(r["_distance"]),
    } for _, r in res.iterrows()]
    return {"query": q, "movies": movies, "ms_lance": ms}


# ── /analyze ─────────────────────────────────────────────────────────────────
class AnalyzeReq(BaseModel):
    movie_ids: List[str]

@app.post("/analyze")
def analyze(req: AnalyzeReq):
    movie_ids = req.movie_ids[:40]
    if not movie_ids:
        return {"error": "no movies"}

    t0      = time.time()
    mid_set = set(movie_ids)
    mgids   = ", ".join(f"'Movie[{m}]'" for m in movie_ids)
    g       = new_gremlin()

    try:
        # ── Actor hits via Gremlin groupCount ─────────────────────────
        raw = gquery(g, f"g.V({mgids}).out('FEATURES').groupCount()")
        actor_hits = {}
        if raw and raw[0]:
            for v, cnt in raw[0].items():
                actor_hits[extract_id(v)] = int(cnt)

        # ── Producer hits ─────────────────────────────────────────────
        raw = gquery(g, f"g.V({mgids}).out('PRODUCED_BY').groupCount()")
        prod_hits = {}
        if raw and raw[0]:
            for v, cnt in raw[0].items():
                prod_hits[extract_id(v)] = int(cnt)

        # ── Top actors (hits >= 2): name, total, connected movies ─────
        top_aids = [a for a, h in sorted(actor_hits.items(), key=lambda x: -x[1]) if h >= 2][:8]
        actors_detail = []
        actor_movies_map = {}

        for aid in top_aids:
            try:
                name  = gquery(g, f"g.V('Actor[{aid}]').values('name')")[0]
                total = int(gquery(g, f"g.V('Actor[{aid}]').in('FEATURES').count()")[0])
                # Which cluster movies is this actor in?
                all_m = gquery(g, f"g.V('Actor[{aid}]').in('FEATURES').id()")
                cluster_m = [extract_id(v) for v in all_m if extract_id(v) in mid_set]
                actor_movies_map[aid] = cluster_m
                hits = actor_hits[aid]
                if total >= 4:
                    actors_detail.append({
                        "id": aid, "name": name,
                        "hits": hits, "total": total,
                        "signal": round(100.0 * hits / total, 1),
                    })
            except Exception as e:
                print(f"  actor detail err {aid}: {e}")

        actors_detail.sort(key=lambda x: (-x["signal"], -x["hits"]))

        # ── Top producers ─────────────────────────────────────────────
        top_pids = [p for p, h in sorted(prod_hits.items(), key=lambda x: -x[1]) if h >= 2][:8]
        prods_detail = []
        prod_movies_map = {}

        for pid in top_pids:
            try:
                name  = gquery(g, f"g.V('Producer[{pid}]').values('name')")[0]
                total = int(gquery(g, f"g.V('Producer[{pid}]').in('PRODUCED_BY').count()")[0])
                all_m = gquery(g, f"g.V('Producer[{pid}]').in('PRODUCED_BY').id()")
                cluster_m = [extract_id(v) for v in all_m if extract_id(v) in mid_set]
                prod_movies_map[pid] = cluster_m
                hits = prod_hits[pid]
                if total > 0:
                    prods_detail.append({
                        "id": pid, "name": name,
                        "hits": hits, "total": total,
                        "signal": round(100.0 * hits / total, 1),
                    })
            except Exception as e:
                print(f"  producer detail err {pid}: {e}")

        prods_detail.sort(key=lambda x: (-x["signal"], -x["hits"]))

        # ── Movie titles (only for movies visible in the graph) ───────
        visible_mids = set()
        for a in actors_detail:
            visible_mids.update(actor_movies_map.get(a["id"], []))
        for p in prods_detail:
            visible_mids.update(prod_movies_map.get(p["id"], []))

        movie_titles = {}
        for mid in visible_mids:
            try:
                r = gquery(g, f"g.V('Movie[{mid}]').values('title')")
                movie_titles[mid] = r[0] if r else mid
            except:
                movie_titles[mid] = mid

        # ── Chemistry pairs (from Gremlin data, computed in Python) ───
        movie_actor_sets = defaultdict(set)
        for aid, mids in actor_movies_map.items():
            for mid in mids:
                movie_actor_sets[mid].add(aid)

        pair_counts = defaultdict(int)
        for mid, actor_set in movie_actor_sets.items():
            for a, b in combinations(sorted(actor_set), 2):
                pair_counts[(a, b)] += 1
        pair_counts = {k: v for k, v in pair_counts.items() if v >= 2}
        candidates = sorted(pair_counts.items(), key=lambda kv: -kv[1])[:4]

        actor_name_map = {a["id"]: a["name"] for a in actors_detail}
        best_pair = None
        for (a, b), cluster_hits in candidates:
            try:
                # Total co-appearances via Gremlin traversal
                r = gquery(g, f"""
                    g.V('Actor[{a}]').in('FEATURES')
                     .where(out('FEATURES').hasId('Actor[{b}]'))
                     .count()
                """)
                total = int(r[0]) if r else cluster_hits
                sig = round(100.0 * cluster_hits / total, 1) if total else 0
                pair = {
                    "actor1": actor_name_map.get(a, a),
                    "actor2": actor_name_map.get(b, b),
                    "together": cluster_hits, "total_together": total, "signal": sig,
                }
                if best_pair is None or sig > best_pair["signal"]:
                    best_pair = pair
            except Exception as e:
                print(f"  pair query err: {e}")

        # ── Gems count via Gremlin ────────────────────────────────────
        gems_count = 0
        if prods_detail:
            try:
                pid_list = [p["id"] for p in prods_detail]
                pgids = ", ".join(f"'Producer[{pid}]'" for pid in pid_list)
                r = gquery(g, f"g.V({pgids}).in('PRODUCED_BY').dedup().count()")
                gems_count = max(0, int(r[0]) - len(visible_mids)) if r else 0
            except Exception as e:
                print(f"  gems count err: {e}")

    finally:
        g.close()

    # ── Build graph response ──────────────────────────────────────────
    nodes, edges = [], []
    for a in actors_detail:
        nodes.append({"id": "A_"+a["id"], "label": a["name"], "type": "actor",
                      "hits": a["hits"], "total": a["total"], "signal": a["signal"]})
        for mid in actor_movies_map.get(a["id"], []):
            edges.append({"source": "A_"+a["id"], "target": "M_"+mid})

    for p in prods_detail:
        nodes.append({"id": "P_"+p["id"], "label": p["name"], "type": "producer",
                      "hits": p["hits"], "total": p["total"], "signal": p["signal"]})
        for mid in prod_movies_map.get(p["id"], []):
            edges.append({"source": "P_"+p["id"], "target": "M_"+mid})

    for mid, title in movie_titles.items():
        nodes.append({"id": "M_"+mid, "label": title, "type": "movie"})

    # ── Insights ──────────────────────────────────────────────────────
    insights = []
    if prods_detail:
        tp = prods_detail[0]
        pct = round(100 * tp["hits"] / tp["total"])
        insights.append({
            "kind": "auteur",
            "text": (f"{tp['name']} produced {tp['hits']} of the {len(movie_ids)} films "
                     f"in this cluster. That is {pct}% of their {tp['total']}-film catalog."),
        })
    if best_pair:
        bp = best_pair
        if bp["together"] == bp["total_together"]:
            insights.append({
                "kind": "chemistry",
                "text": (f"{bp['actor1']} & {bp['actor2']} co-starred "
                         f"{bp['together']} times. Every collaboration is in this cluster."),
            })
        else:
            insights.append({
                "kind": "chemistry",
                "text": (f"{bp['actor1']} & {bp['actor2']} co-starred "
                         f"{bp['together']} times here ({bp['signal']}% of "
                         f"{bp['total_together']} total collaborations)."),
            })
    if actors_detail and actors_detail[0]["signal"] >= 20:
        ta = actors_detail[0]
        insights.append({
            "kind": "actor",
            "text": (f"{ta['name']} appears in {ta['hits']} of these films, "
                     f"{ta['signal']}% of their {ta['total']}-film career."),
        })

    return {
        "nodes": nodes, "edges": edges,
        "insights": insights, "gems_count": gems_count,
        "ms_graph": round((time.time() - t0) * 1000),
    }


# ── /discover ────────────────────────────────────────────────────────────────
class DiscoverReq(BaseModel):
    movie_ids: List[str]
    query: str

SIGNAL_THRESHOLD = 10.0  # percent of catalog that must overlap the cluster
MIN_HITS         = 2     # creator must have at least this many cluster films


def _leader_records(g, hits_map, label, edge, kind):
    """
    Turn a {creator_id: hits_in_cluster} map into ranked leader records.
    Only keeps creators whose catalog concentration meets SIGNAL_THRESHOLD.
    """
    leaders = []
    for cid, hits in hits_map.items():
        if hits < MIN_HITS:
            continue
        try:
            name  = gquery(g, f"g.V('{label}[{cid}]').values('name')")[0]
            total = int(gquery(g, f"g.V('{label}[{cid}]').in('{edge}').count()")[0])
            if total <= 0:
                continue
            signal = round(100.0 * hits / total, 1)
            if signal < SIGNAL_THRESHOLD:
                continue
            leaders.append({
                "id": cid, "name": name, "kind": kind,
                "label_v": label, "edge": edge,
                "hits": hits, "total": total, "signal": signal,
            })
        except Exception as e:
            print(f"  {kind} detail err {cid}: {e}")
    return leaders


@app.post("/discover")
def discover(req: DiscoverReq):
    """
    Find films the graph reveals that LanceDB alone can't.

    1. LanceDB wide search defines the semantic universe (films search CAN see)
    2. Gremlin groupCount over PRODUCED_BY and FEATURES finds creators with strong
       concentration in your cluster (signal = hits / total_catalog)
    3. Gremlin hops from those creators back to their OTHER films
    4. Those graph-only candidates are re-embedded and re-ranked by LanceDB
    """
    movie_ids = req.movie_ids[:40]
    query     = req.query.strip()
    if not movie_ids or not query:
        return {"error": "missing params"}

    t0 = time.time()

    # 1. LanceDB defines the semantic universe
    vec  = get_model().encode(query).tolist()
    wide = get_tbl().search(vec).limit(500).to_pandas()
    semantic_universe = set(wide["movie_id"].tolist())

    mgids = ", ".join(f"'Movie[{m}]'" for m in movie_ids)
    g     = new_gremlin()

    try:
        # 2. Cluster-side hit counts (one Gremlin call each)
        raw = gquery(g, f"g.V({mgids}).out('PRODUCED_BY').groupCount()")
        prod_hits = {extract_id(v): int(cnt) for v, cnt in (raw[0].items() if raw and raw[0] else {}.items())}

        raw = gquery(g, f"g.V({mgids}).out('FEATURES').groupCount()")
        actor_hits = {extract_id(v): int(cnt) for v, cnt in (raw[0].items() if raw and raw[0] else {}.items())}

        # 3. Filter by signal strength, not raw hit count
        prod_leaders  = _leader_records(g, prod_hits,  "Producer", "PRODUCED_BY", "producer")
        actor_leaders = _leader_records(g, actor_hits, "Actor",    "FEATURES",    "actor")

        leaders = sorted(prod_leaders + actor_leaders, key=lambda x: -x["signal"])[:8]

        if not leaders:
            return {
                "graph_finds": [], "cluster_leaders": [],
                "insight": (f"No creators have meaningful concentration in this cluster "
                            f"(threshold: {SIGNAL_THRESHOLD:.0f}% of catalog). "
                            f"LanceDB captured the creative signal fully here."),
                "ms": round((time.time() - t0) * 1000),
            }

        # 4. Hop to each leader's OTHER films; also collect the bridge films
        #    (the cluster films that put the leader on our radar in the first place)
        mid_set    = set(movie_ids)
        graph_only = []       # [(movie_id, leader_record)]
        seen = set()
        for leader in leaders:
            try:
                r = gquery(g, f"g.V('{leader['label_v']}[{leader['id']}]').in('{leader['edge']}').id()")
                bridge_ids = []
                for v in r:
                    mid = extract_id(v)
                    if mid in mid_set:
                        bridge_ids.append(mid)
                    elif mid not in semantic_universe and mid not in seen:
                        graph_only.append((mid, leader))
                        seen.add(mid)
                leader["bridge_ids"] = bridge_ids[:5]
            except Exception as e:
                print(f"  leader hop err: {e}")

        # Batch-fetch titles for every bridge film we touched
        all_bridge_ids = list({bid for l in leaders for bid in l.get("bridge_ids", [])})
        bridge_titles = {}
        for bid in all_bridge_ids:
            try:
                bridge_titles[bid] = gquery(g, f"g.V('Movie[{bid}]').values('title')")[0]
            except Exception:
                pass

        for leader in leaders:
            leader["bridge"] = [
                {"movie_id": bid, "title": bridge_titles[bid]}
                for bid in leader.get("bridge_ids", [])
                if bid in bridge_titles
            ]

        graph_only = graph_only[:80]

        if not graph_only:
            return {
                "graph_finds": [],
                "cluster_leaders": [_leader_brief(l) for l in leaders],
                "insight": (f"All films by your {len(leaders)} top creators are already "
                            f"within semantic search range. LanceDB and PuppyGraph agree."),
                "ms": round((time.time() - t0) * 1000),
            }

        # 5. Fetch movie details for the graph-only candidates
        graph_movies = []
        for mid, leader in graph_only:
            try:
                title    = gquery(g, f"g.V('Movie[{mid}]').values('title')")[0]
                overview = gquery(g, f"g.V('Movie[{mid}]').values('overview')")[0]
                genres   = gquery(g, f"g.V('Movie[{mid}]').values('genres')")[0]
                if overview:
                    graph_movies.append({
                        "movie_id": mid, "title": title, "overview": overview,
                        "genres": genres or "", "leader": leader,
                    })
            except:
                pass

    finally:
        g.close()

    if not graph_movies:
        return {
            "graph_finds": [],
            "cluster_leaders": [_leader_brief(l) for l in leaders],
            "insight": "No graph-exclusive films with usable plot descriptions found.",
            "ms": round((time.time() - t0) * 1000),
        }

    # 6. LanceDB re-rank
    overviews  = [m["overview"] for m in graph_movies]
    q_vec      = np.array(get_model().encode(query))
    movie_vecs = get_model().encode(overviews, show_progress_bar=False)
    norms      = np.linalg.norm(movie_vecs, axis=1, keepdims=True)
    movie_vecs = movie_vecs / np.clip(norms, 1e-9, None)
    q_norm     = q_vec / np.clip(np.linalg.norm(q_vec), 1e-9, None)
    sims       = (movie_vecs @ q_norm).tolist()

    scored = sorted(zip(graph_movies, sims), key=lambda x: -x[1])[:20]

    graph_finds = []
    for row, sim in scored:
        l = row["leader"]
        verb = "produced" if l["kind"] == "producer" else "starred in"
        why_short = f"via {l['name']} ({l['signal']}% of {l['total']} films in dataset)"
        why_long  = (f"{l['name']} {verb} {l['hits']} of your {len(movie_ids)} top matches. "
                     f"That is {l['signal']}% of the {l['total']} films of theirs in this dataset, "
                     f"a stronger signal than {SIGNAL_THRESHOLD:.0f}% threshold required.")
        graph_finds.append({
            "movie_id":       row["movie_id"],
            "title":          row["title"],
            "overview":       row["overview"],
            "genres":         row["genres"],
            "leader_id":      l["id"],
            "leader_name":    l["name"],
            "leader_kind":    l["kind"],
            "leader_hits":    l["hits"],
            "leader_total":   l["total"],
            "leader_signal":  l["signal"],
            "leader_label":   l["label_v"],
            "leader_edge":    l["edge"],
            "bridge_films":   l.get("bridge", []),
            "cluster_size":   len(movie_ids),
            "similarity":     round(sim * 100, 1),
            "why":            why_short,
            "why_long":      why_long,
        })

    top_sim = graph_finds[0]["similarity"] if graph_finds else 0
    insight = (
        f"LanceDB scanned {len(semantic_universe):,} films by description and gave you "
        f"the top {len(movie_ids)} matches. PuppyGraph kept only creators who had at "
        f"least {SIGNAL_THRESHOLD:.0f}% of their catalog among those matches, then "
        f"hopped to their other films. The closest is {top_sim:.0f}% aligned with your "
        f"query but never surfaced in semantic search."
    )

    return {
        "graph_finds":     graph_finds,
        "cluster_leaders": [_leader_brief(l) for l in leaders],
        "signal_threshold": SIGNAL_THRESHOLD,
        "insight":         insight,
        "ms":              round((time.time() - t0) * 1000),
    }


def _leader_brief(l):
    return {
        "name":   l["name"],
        "kind":   l["kind"],
        "hits":   l["hits"],
        "total":  l["total"],
        "signal": l["signal"],
    }


if __name__ == "__main__":
    os.makedirs("demo_ui", exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=8052)
