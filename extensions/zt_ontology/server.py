"""ZT add-on: read-only ontology hypergraph tools for Vibe-Trading.

Opt-in stdio MCP server (same pattern as extensions/pit_actor_sim).  It reads
the Investment project's canonical ontology hypergraph -- the pitdb tables
dim_concept, dim_label, dim_node, fact_hyperedge, fact_hyperedge_member and
ontology_release stored as parquet in the G: lake -- into an IN-MEMORY DuckDB
and answers through the project's own query engine (pitdb/ontology_query.py).

It never writes: no database file, no spill files (DuckDB temp_directory is
disabled), no bytecode caches in the canonical tree, no graph appends.  It adds
no broker connector and no order path; order verbs resolve to approval-only
proposals and nothing here can execute them.

Semantics every tool keeps:
  * every call takes an explicit as-of instant with a timezone offset;
  * labels resolve through versioned aliases valid at that instant (never by
    matching one literal string); ambiguous or unknown phrases are reported,
    not guessed;
  * a missing edge is UNKNOWN, not false (NONE only under a closed-scope
    census observation);
  * outputs are capped at 500 rows and contain no probabilities.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True   # never write __pycache__ into the canonical G: tree

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("zt-ontology")

MAX_ROWS = 500
AUTHORITY = ("READ_ONLY_RESEARCH: ontology release draft; graph answers carry no "
             "probabilities and no execution authority")
ABSENT_EDGE_SEMANTICS = "UNKNOWN unless a closed_scope_census observation covers it"
ONTOLOGY_TABLES = ("dim_concept", "dim_label", "dim_node", "fact_hyperedge",
                   "fact_hyperedge_member", "ontology_release")


def project() -> Path:
    raw = os.environ.get("INVESTMENT_AI_PROJECT_ROOT")
    if not raw:
        raise RuntimeError("INVESTMENT_AI_PROJECT_ROOT is required")
    root = Path(raw).resolve(strict=True)
    if root.name != "Investment-AI-Drive-Research" or root.parent.name != "work":
        raise ValueError("Use the canonical work project folder")
    for part in ("AGENTS.md", "implementation/pit_warehouse/pitdb/ontology_query.py",
                 "implementation/pit_warehouse/pitdb/ontology_schema.sql"):
        if not (root / part).exists():
            raise FileNotFoundError(part)
    return root


def engine():
    """The project's ontology engine (pitdb.ontology_query / ontology_loader)."""
    path = str(project() / "implementation" / "pit_warehouse")
    if path not in sys.path:
        sys.path.insert(0, path)
    from pitdb import ontology_loader, ontology_query
    return ontology_loader, ontology_query


def lake_root() -> Path:
    raw = os.environ.get("PITDB_LAKE")
    return Path(raw) if raw else project() / "implementation" / "pit_warehouse" / "lake"


def lake_signature() -> dict:
    files = {}
    for table in ONTOLOGY_TABLES:
        path = lake_root() / table / "data.parquet"
        try:
            stat = path.stat()
            files[table] = [stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            files[table] = None
    return {"lake_root": str(lake_root()), "files": files}


_STORE: dict = {"signature": None, "con": None}
_LOCK = threading.Lock()


def store():
    """In-memory read-only copy of the ontology tables, re-hydrated when the lake changes."""
    with _LOCK:
        signature = lake_signature()
        if _STORE["con"] is None or _STORE["signature"] != signature:
            L, _ = engine()
            con = L.memory_connection()
            counts = L.hydrate_ontology_from_lake(con, lake_root())
            if not counts.get("dim_concept"):
                con.close()
                raise RuntimeError("no ontology release in the lake; load it with "
                                   "bin/seed_ontology.py --apply (see README)")
            if _STORE["con"] is not None:
                _STORE["con"].close()
            _STORE.update(signature=signature, con=con)
        return _STORE["con"]


def asof_utc(raw: str) -> datetime:
    """Explicit, past, offset-qualified instant -> naive UTC (pitdb convention)."""
    value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("asof requires a timezone offset, e.g. 2026-09-21T00:00:00Z")
    value = value.astimezone(timezone.utc)
    if value > datetime.now(timezone.utc):
        raise ValueError("asof cannot be in the future")
    return value.replace(tzinfo=None)


def row_limit(value: int) -> int:
    if not 1 <= int(value) <= MAX_ROWS:
        raise ValueError(f"limit must be 1..{MAX_ROWS}")
    return int(value)


def _cap(obj, limit: int = MAX_ROWS):
    """Cap every list in a response (rows, notes, member lists) at `limit` items."""
    if isinstance(obj, dict):
        return {k: _cap(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_cap(v, limit) for v in obj[:limit]]
    return obj


def respond(result: dict, limit: int = MAX_ROWS) -> dict:
    L, Q = engine()
    out = json.loads(json.dumps(result, default=str, ensure_ascii=False))
    out = _cap(out, limit)
    out.setdefault("authority", AUTHORITY)
    out.setdefault("absent_edge_semantics", ABSENT_EDGE_SEMANTICS)
    out["releases"] = json.loads(json.dumps(Q.active_releases(store()), default=str))
    return out


@mcp.tool
def onto_concept(label_or_id: str, asof: str) -> dict:
    """Resolve an ontology term (concept ID or any versioned alias, any language)
    at an as-of instant and return its versioned record: definition, domain,
    valid window, labels valid at asof, deprecated labels, predecessor/successor,
    broader/narrower concepts, relations it participates in and why it exists.
    Verbs return their contract (order verbs are approval-only proposals)."""
    at = asof_utc(asof)
    if not str(label_or_id).strip():
        raise ValueError("label_or_id is required")
    _, Q = engine()
    return respond(Q.concept_record(store(), label_or_id, at))


@mcp.tool
def onto_node(entity: str, asof: str) -> dict:
    """Resolve an entity (node ID such as SEC:1 or ENT:0001045810, or a
    time-scoped label such as a ticker or a name) to a graph node as known and
    valid at asof, with its typed attributes, provenance and evidence grade.
    Nodes first known after asof are reported as UNKNOWN_AT_ASOF."""
    at = asof_utc(asof)
    if not str(entity).strip():
        raise ValueError("entity is required")
    _, Q = engine()
    return respond(Q.node_record(store(), entity, at))


@mcp.tool
def onto_neighbors(node: str, asof: str, edge_types: list[str] | None = None,
                   hops: int = 1, limit: int = MAX_ROWS) -> dict:
    """Hyperedges around a node (hops 1 or 2) known and valid at asof.
    edge_types are relation labels or IDs (e.g. 'holds', 'issued by',
    'business link'); each is resolved through versioned aliases.  For every
    requested type with no edge the status is UNKNOWN (NONE only under a
    closed-scope census) -- absence is never reported as false."""
    at = asof_utc(asof)
    if not 1 <= int(hops) <= 2:
        raise ValueError("hops must be 1 or 2")
    limit = row_limit(limit)
    if edge_types is not None and (not isinstance(edge_types, list) or len(edge_types) > 20):
        raise ValueError("edge_types must be a list of at most 20 labels or IDs")
    _, Q = engine()
    return respond(Q.neighbors(store(), node, at, edge_types or None, int(hops), limit), limit)


@mcp.tool
def onto_actor_roster(scenario_id: str, asof: str, limit: int = MAX_ROWS) -> dict:
    """Actors of an MCT scenario (e.g. 'iran_oil_pilot') at asof, grouped into
    the two families -- real_economy (issuers, policy/state actors, producers)
    and security_flow (funds, ETF complexes, dealers, the operator) -- with
    holdings (or UNKNOWN / absence-of-disclosure), visibility (information
    set), observables that reveal them, constraints and funding links."""
    at = asof_utc(asof)
    if not str(scenario_id).strip():
        raise ValueError("scenario_id is required")
    limit = row_limit(limit)
    _, Q = engine()
    return respond(Q.actor_roster(store(), scenario_id, at, limit), limit)


@mcp.tool
def onto_competency(question_id: str, asof: str, params: dict | None = None,
                    limit: int = MAX_ROWS) -> dict:
    """Answer one of the finance competency questions (CQ-FIN-01..11, or the
    question's text) from the graph as known at asof.  Optional params narrow
    the question, e.g. {"holder": "PORT:..."} for CQ-FIN-01/07,
    {"opportunity": "..."} for CQ-FIN-09, {"since": "<ISO>"} for CQ-FIN-05/08.
    Answers carry status ANSWERED | NONE | UNKNOWN and no probabilities;
    rankings are structural triage and say so."""
    at = asof_utc(asof)
    limit = row_limit(limit)
    params = dict(params or {})
    if len(params) > 10 or not all(isinstance(k, str) and isinstance(v, (str, int, float))
                                   for k, v in params.items()):
        raise ValueError("params must be a small map of names to strings or numbers")
    _, Q = engine()
    return respond(Q.answer_cq(store(), question_id, at, params, limit), limit)


if __name__ == "__main__":
    mcp.run()
