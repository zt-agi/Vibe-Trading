# ZT add-on: ontology hypergraph extension

Opt-in, read-only MCP server that gives Vibe-Trading access to ZT's ontology
hypergraph for the Investment-AI-Drive-Research project. It is an add-on: it
does not change, disable or replace any Vibe-Trading feature, adds no broker
connector and has no order path.

## What it reads

The canonical graph is the pitdb bitemporal hypergraph in the project's G:
lake (`implementation/pit_warehouse/lake/<table>/data.parquet`):
`dim_concept`, `dim_label`, `dim_node`, `fact_hyperedge`,
`fact_hyperedge_member`, `ontology_release`. The server hydrates those six
tables into an in-memory DuckDB (spilling disabled), re-hydrates when the
parquet files change, and answers through the project's own engine
`implementation/pit_warehouse/pitdb/ontology_query.py` - the same code the
pitdb tests, the replay gate and the seed script use. Nothing is written:
no database file, no temp files, no graph rows.

Ontology release: `INVESTMENT.ONTOLOGY.FINANCE_MVO@0.1.0` (draft, project
research stage) under `ontology/finance_mvo/` in the project.

## Tools

| Tool | Purpose |
|---|---|
| `onto_concept(label_or_id, asof)` | Versioned concept record; labels resolve through aliases valid at `asof` |
| `onto_node(entity, asof)` | Node by ID or time-scoped label (ticker, name, CIK) |
| `onto_neighbors(node, asof, edge_types, hops<=2)` | Hyperedges around a node; missing edge types are UNKNOWN |
| `onto_actor_roster(scenario_id, asof)` | Scenario actors in two families (`real_economy`, `security_flow`) with holdings and visibility |
| `onto_competency(question_id, asof, params)` | Answers CQ-FIN-01..11 |

Every call needs an explicit past `asof` with a timezone offset. Responses are
capped at 500 rows per list, carry `authority`, `absent_edge_semantics` and
the active release, and contain no probabilities.

## Operator configuration (PC1)

ZT 2026-09-28: runtime on E:, canonical data on G:, nothing on C: or D:.

~~~json
{
  "mcpServers": {
    "zt-ontology": {
      "command": "E:\\codex-runtime\\investment-ai\\vibe-trading\\venv\\Scripts\\python.exe",
      "args": ["-B", "E:\\codex-runtime\\investment-ai\\vibe-trading\\src\\extensions\\zt_ontology\\server.py"],
      "env": {
        "INVESTMENT_AI_PROJECT_ROOT": "G:\\My Drive\\work\\Investment-AI-Drive-Research",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "E:\\codex-runtime\\investment-ai\\vibe-trading\\home\\pycache"
      },
      "enabledTools": [
        "onto_concept",
        "onto_node",
        "onto_neighbors",
        "onto_actor_roster",
        "onto_competency"
      ],
      "toolTimeout": 120
    }
  }
}
~~~

Optional: `PITDB_LAKE` points at a different lake folder (read only).

Before first use, load the release and seed the graph once from
`implementation/pit_warehouse` (dry run first, it prints counts):

~~~text
python -B bin\seed_ontology.py
python -B bin\seed_ontology.py --apply
~~~

`--apply` writes only the six ontology parquet files into the G: lake; run it
when no other pitdb writer is active. The companion skill
`ontology-hypergraph` (E: runtime profile, `skills/user/`) tells the agent
when and how to use these tools.

## Tests

~~~text
set INVESTMENT_AI_PROJECT_ROOT=G:\My Drive\work\Investment-AI-Drive-Research
set ZT_ONTOLOGY_SKILL_DIR=E:\codex-runtime\investment-ai\vibe-trading\profile\.vibe-trading\skills\user\ontology-hypergraph
python -B -m unittest discover -s extensions/zt_ontology -p "test_*.py" -v
~~~

`test_server.py` answers every competency question from the release's
synthetic replay fixture, checks paraphrased and deprecated labels, the as-of
cut (an edge known at t1 is invisible at t0), UNKNOWN versus NONE, output caps,
negative controls and that no tool changes the store. `test_skill_loader.py`
loads the skill through Vibe-Trading's own `SkillsLoader`. Without the
private project the suites skip.
