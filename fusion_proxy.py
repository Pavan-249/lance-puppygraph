import re
import sys
import logging
import argparse
from pathlib import Path

import yaml
import duckdb
from sentence_transformers import SentenceTransformer
from buenavista.backends.duckdb import DuckDBConnection, DuckDBSession
from buenavista.postgres import BuenaVistaServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fusion_proxy")

VEC_SEARCH_RE = re.compile(
    r"vec_search\(\s*(\w+)\s*,\s*['\"]+(.+?)['\"]+\s*,\s*(\d+)\s*\)",
    re.IGNORECASE,
)


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        log.error(f"Config not found: {p}")
        sys.exit(1)
    with open(p) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("proxy", {})
    cfg["proxy"].setdefault("host", "0.0.0.0")
    cfg["proxy"].setdefault("port", 5433)
    cfg.setdefault("duckdb", {}).setdefault("path", "data.db")
    cfg.setdefault("lancedb", {}).setdefault("path", "./lance_data")
    cfg.setdefault("embedding", {}).setdefault("model", "BAAI/bge-small-en-v1.5")
    cfg.setdefault("catalog", {}).setdefault("name", "graph_data")
    cfg.setdefault("vector_search", {}).setdefault("id_column", "id")
    cfg["vector_search"].setdefault("vector_column", "vector")
    return cfg


def _rewrite_catalog(sql: str, catalog: str) -> str:
    return re.sub(r'(?<![.\w])public\.(?!pg_)', f'{catalog}.public.', sql)


def _lance_subquery(table: str, query_text: str, k: int, model, id_col: str, vec_col: str) -> str:
    vec = model.encode(query_text).tolist()
    vec_literal = "[" + ", ".join(str(v) for v in vec) + "]::FLOAT[]"
    return (
        f"(SELECT {id_col} FROM lance_vector_search("
        f"'lance_ns.main.{table}', '{vec_col}', {vec_literal}, k={k}))"
    )


class FusionSession(DuckDBSession):
    def __init__(self, cursor, model, catalog, id_col, vec_col):
        super().__init__(cursor)
        self._model = model
        self._catalog = catalog
        self._id_col = id_col
        self._vec_col = vec_col

    def execute_sql(self, sql, params=None):
        sql = _rewrite_catalog(sql, self._catalog)
        upper = sql.upper()

        # HACK 0: silence JDBC probes DuckDB doesn't support
        if "SHOW TRANSACTION ISOLATION LEVEL" in upper:
            return super().execute_sql("SELECT 'read committed' AS transaction_isolation", params)
        if upper.startswith("SET "):
            return super().execute_sql("SELECT 1", params)

        # HACK 1: DuckDB pg_catalog is incomplete, INNER JOIN fails on unknown OIDs
        if "JOIN pg_catalog.pg_type t ON (a.atttypid = t.oid)" in sql:
            sql = sql.replace(
                "JOIN pg_catalog.pg_type t ON (a.atttypid = t.oid)",
                "LEFT JOIN pg_catalog.pg_type t ON (a.atttypid = t.oid)",
            )
            return super().execute_sql(sql, params)

        # HACK 1.5: PuppyGraph uses array_upper which duckdb doesn't support natively in this context
        if "array_upper(current_schemas(false), 1)" in sql:
            sql = sql.replace("array_upper(current_schemas(false), 1)", "1")
            return super().execute_sql(sql, params)

        # HACK 2: intercept vec_search(table, query, k) and rewrite to lance_vector_search
        match = None
        param_index = -1

        if params:
            for i, p in enumerate(params):
                if isinstance(p, str) and "vec_search(" in p:
                    match = VEC_SEARCH_RE.search(p)
                    if match:
                        param_index = i
                        break

        if not match:
            match = VEC_SEARCH_RE.search(sql)

        if match:
            table, query_text, k = match.groups()
            query_text = query_text.replace("''", "'")
            k = int(k)
            log.info(f"[INTERCEPT] table='{table}' query='{query_text}' k={k}")
            subquery = _lance_subquery(table, query_text, k, self._model, self._id_col, self._vec_col)

            if param_index >= 0:
                rewritten = re.sub(
                    r"=\s*(?:\$" + str(param_index + 1) + r"|\?)",
                    f"IN {subquery}", sql, flags=re.IGNORECASE,
                )
                new_params = list(params)
                new_params.pop(param_index)
                return super().execute_sql(rewritten, new_params)
            else:
                rewritten = re.sub(
                    r"IN\s*\(\s*vec_search\(.*?\)\s*\)",
                    f"IN {subquery}", sql, flags=re.IGNORECASE,
                )
                if rewritten == sql:
                    rewritten = re.sub(
                        r"=\s*['\"]?vec_search\(.*?\)['\"]?",
                        f"IN {subquery}", sql, flags=re.IGNORECASE,
                    )
                return super().execute_sql(rewritten, params)

        return super().execute_sql(sql, params)


class FusionConnection(DuckDBConnection):
    def __init__(self, db, model, catalog, id_col, vec_col):
        super().__init__(db)
        self._model = model
        self._catalog = catalog
        self._id_col = id_col
        self._vec_col = vec_col

    def new_session(self):
        cur = self.db.cursor()
        cur.execute(f"USE {self._catalog}")
        cur.execute(f"SET search_path='{self._catalog}.public'")
        return FusionSession(cur, self._model, self._catalog, self._id_col, self._vec_col)


class QuietBuenaVistaServer(BuenaVistaServer):
    def handle_error(self, request, client_address):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg_dir = Path(args.config).resolve().parent
    host = cfg["proxy"]["host"]
    port = cfg["proxy"]["port"]
    duckdb_path = str(cfg_dir / cfg["duckdb"]["path"])
    lance_path  = str(cfg_dir / cfg["lancedb"]["path"])
    catalog = cfg["catalog"]["name"]
    id_col = cfg["vector_search"]["id_column"]
    vec_col = cfg["vector_search"]["vector_column"]

    if not Path(duckdb_path).exists():
        log.error(f"DuckDB file not found: {duckdb_path}")
        sys.exit(1)
    if not Path(lance_path).exists():
        log.error(f"LanceDB path not found: {lance_path}")
        sys.exit(1)

    log.info(f"Loading model: {cfg['embedding']['model']}")
    model = SentenceTransformer(cfg["embedding"]["model"])

    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{duckdb_path}' AS {catalog}")
    con.execute(f"USE {catalog}")
    con.execute("CREATE SCHEMA IF NOT EXISTS public")
    con.execute("INSTALL lance")
    con.execute("LOAD lance")
    con.execute(f"ATTACH '{lance_path}' AS lance_ns (TYPE LANCE)")

    server = QuietBuenaVistaServer(
        (host, port),
        FusionConnection(con, model, catalog, id_col, vec_col),
    )
    log.info(f"FusionProxy on {host}:{port} | catalog={catalog} | db={duckdb_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
