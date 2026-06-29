"""Fetch the last (or a specific) Langfuse trace and dump its full content.

Usage:
    python scripts/fetch_langfuse_traces.py             # last trace
    python scripts/fetch_langfuse_traces.py --last 5    # print last 5 (summary)
    python scripts/fetch_langfuse_traces.py --id <id>   # a specific trace id

The full content of the last/specified trace (generations input+output, spans,
attached errors) is printed to stdout AND written to
``examples/<EXAMPLE_NAME>/intermediate/last_trace_dump.json`` so it can be
read back for offline debugging.

Designed for the iterative debugging workflow described in README.md:
    run pipeline → open in PBI → paste error → (this script) → diagnose → fix
"""
import argparse
import json
import os
import pathlib
import sys

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault(
    "LANGFUSE_HOST",
    os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
)

ROOT = pathlib.Path(__file__).parent.parent
EXAMPLE = os.getenv("EXAMPLE_NAME", "")

try:
    from langfuse import Langfuse
except ImportError:
    print("langfuse not installed. Run: pip install langfuse")
    sys.exit(1)

_lf = Langfuse()


# ---------------------------------------------------------------------------
# Helpers that cope with Langfuse SDK version differences
# ---------------------------------------------------------------------------

def _get_traces(limit: int = 1, trace_id: str | None = None):
    if trace_id:
        # newer SDKs expose .get_trace; older ones .fetch_trace
        for meth in ("get_trace", "fetch_trace"):
            fn = getattr(_lf, meth, None)
            if fn:
                try:
                    res = fn(trace_id)
                except TypeError:
                    res = fn(id=trace_id)
                return res
    # list-style fetch
    for meth in ("get_traces", "fetch_traces"):
        fn = getattr(_lf, meth, None)
        if fn:
            return fn(limit=limit)
    # fallback: observations
    obs = _lf.fetch_observations(limit=1, type="span")
    if obs and obs.data:
        return type("X", (), {"data": [obs.data[0].trace]})()
    return None


def _observations_for(trace):
    obs = getattr(trace, "observations", None)
    if obs:
        return obs
    tid = getattr(trace, "id", None)
    if not tid:
        return []
    res = _lf.fetch_observations(trace_id=tid)
    return res.data if res else []


def _as_dict(obj) -> dict:
    """Best-effort conversion of a Langfuse observation/trace to a plain dict."""
    if isinstance(obj, dict):
        return obj
    data: dict = {}
    for k in ("id", "name", "type", "start_time", "end_time", "input", "output",
              "model", "usage", "metadata", "level", "status", "parent_observation_id"):
        v = getattr(obj, k, None)
        if v is not None:
            data[k] = v
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--last", type=int, default=1, help="Number of recent traces to show (summary). Default 1 (full dump).")
    p.add_argument("--id", default=None, help="Fetch a specific trace id.")
    args = p.parse_args()

    if args.id:
        trace = _get_traces(trace_id=args.id)
        traces = type("X", (), {"data": [trace]})()
    else:
        traces = _get_traces(limit=args.last)

    if not traces or not getattr(traces, "data", None):
        print("No traces found. Run the pipeline first with LANGFUSE_ENABLED=true.")
        sys.exit(0)

    # --- Summary of last N ---
    if args.last > 1 and not args.id:
        print(f"Last {len(traces.data)} traces:\n")
        for t in traces.data:
            tid = getattr(t, "id", "?")
            tname = getattr(t, "name", "?")
            tstart = getattr(t, "start_time", "?")
            tmeta = getattr(t, "metadata", {}) or {}
            print(f"  {tstart}  {tname}  id={tid}  meta={tmeta}")
        print(f"\nFor a full dump: python scripts/fetch_langfuse_traces.py --id {getattr(traces.data[0], 'id', '')}")
        return

    # --- Full dump of the single trace ---
    trace = traces.data[0]
    tid = getattr(trace, "id", "?")
    tname = getattr(trace, "name", "?")
    tmeta = getattr(trace, "metadata", {}) or {}
    tstart = getattr(trace, "start_time", "?")
    tstatus = getattr(trace, "status", "?")

    observations = _observations_for(trace)

    dump = {
        "trace_id": tid,
        "trace_name": tname,
        "status": tstatus,
        "start_time": str(tstart),
        "metadata": tmeta,
        "observations": [_as_dict(o) for o in observations],
    }

    # ----- stdout -----
    print("=" * 78)
    print(f"TRACE: {tname}   id={tid}")
    print(f"start={tstart}  status={tstatus}")
    print(f"metadata={json.dumps(tmeta, ensure_ascii=False)}")
    print("-" * 78)
    for o in observations:
        od = _as_dict(o)
        otype = od.get("type", "?")
        oname = od.get("name", "?")
        print(f"\n[{otype.upper()}] {oname}")
        if otype == "generation":
            inp = od.get("input")
            if inp is not None:
                _print_block("INPUT", inp)
            out = od.get("output")
            if out is not None:
                _print_block("OUTPUT", out)
            if od.get("model"):
                print(f"  model: {od['model']}")
            if od.get("usage"):
                print(f"  usage: {od['usage']}")
        elif od.get("metadata"):
            md = od["metadata"]
            # Highlight error attachments (from obs.log_errors / log_event)
            if "errors" in md:
                print(f"  errors ({md.get('count', len(md['errors']))}):")
                for e in md["errors"]:
                    print(f"    - {_short(e, 240)}")
            else:
                print(f"  metadata: {json.dumps(md, ensure_ascii=False)[:300]}")
    print("\n" + "=" * 78)
    base = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    print(f"Browser: {base.rstrip('/')}/trace/{tid}")

    # ----- JSON file -----
    out_path = None
    if EXAMPLE:
        out_path = ROOT / "examples" / EXAMPLE / "intermediate" / "last_trace_dump.json"
    else:
        out_path = ROOT / "last_trace_dump.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Full dump written to: {out_path}")


def _print_block(label: str, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    print(f"  --- {label} ---")
    for line in str(text).splitlines():
        print(f"  {line}")


def _short(value, n: int = 200) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


if __name__ == "__main__":
    main()