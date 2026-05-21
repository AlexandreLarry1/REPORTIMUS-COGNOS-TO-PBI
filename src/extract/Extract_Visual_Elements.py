"""Extract sheets, visual objects, and layout from a Qlik app via WebSocket API."""
import argparse
import json
import os
import pathlib
import websocket
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent
WS_URL = os.getenv("QLIK_WS_URL", "ws://localhost:4848/app/")

parser = argparse.ArgumentParser()
parser.add_argument("--app", default=os.getenv("QLIK_QVF_NAME", ""), help="QVF name")
args = parser.parse_args()
QVF_NAME = args.app

EXAMPLE_NAME = os.getenv("EXAMPLE_NAME", "")
OUTPUT_DIR = ROOT / "examples" / EXAMPLE_NAME / "intermediate"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ws = websocket.create_connection(WS_URL)
_req_id = 0


def send(method: str, handle: int, params: list) -> dict:
    global _req_id
    _req_id += 1
    ws.send(json.dumps({"jsonrpc": "2.0", "id": _req_id, "method": method, "handle": handle, "params": params}))
    return recv(_req_id)


def recv(expected_id: int) -> dict:
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == expected_id:
            if "error" in msg:
                raise RuntimeError(f"Qlik error (id={expected_id}): {msg['error']}")
            return msg["result"]


# --- open app ---
doc_list = send("GetDocList", -1, [])["qDocList"]
match = next((d for d in doc_list if d.get("qDocName") == QVF_NAME), None)
if match is None:
    raise RuntimeError(f"App '{QVF_NAME}' not found. Available: {[d.get('qDocName') for d in doc_list]}")

app_handle = send("OpenDoc", -1, [match["qDocId"]])["qReturn"]["qHandle"]
print(f"Opened '{QVF_NAME}' (handle={app_handle})")

# --- load master dimensions ---
dim_handle = send("CreateSessionObject", app_handle, [{
    "qInfo": {"qType": "DimensionList"},
    "qDimensionListDef": {"qType": "dimension", "qData": {"dim": "/qDim"}}
}])["qReturn"]["qHandle"]
dim_items = send("GetLayout", dim_handle, [])["qLayout"].get("qDimensionList", {}).get("qItems", [])
master_dims = {
    d["qInfo"]["qId"]: {
        "field": (d.get("qData", {}).get("dim", {}).get("qFieldDefs") or [""])[0],
        "label": d.get("qMeta", {}).get("title", ""),
    }
    for d in dim_items
}
print(f"Master dimensions: {len(master_dims)}")

# --- load master measures ---
meas_handle = send("CreateSessionObject", app_handle, [{
    "qInfo": {"qType": "MeasureList"},
    "qMeasureListDef": {"qType": "measure", "qData": {"measure": "/qMeasure"}}
}])["qReturn"]["qHandle"]
meas_items = send("GetLayout", meas_handle, [])["qLayout"].get("qMeasureList", {}).get("qItems", [])
master_measures = {
    m["qInfo"]["qId"]: {
        "expression": m.get("qData", {}).get("measure", {}).get("qDef", ""),
        "label":      m.get("qMeta", {}).get("title", ""),
    }
    for m in meas_items
}
print(f"Master measures: {len(master_measures)}")


def resolve_dim(d: dict) -> dict:
    lib_id = d.get("qLibraryId", "")
    if lib_id and lib_id in master_dims:
        return master_dims[lib_id]
    field_defs = d.get("qDef", {}).get("qFieldDefs", [])
    label = d.get("qDef", {}).get("qLabel", "") or (field_defs[0] if field_defs else "")
    return {"field": field_defs[0] if field_defs else "", "label": label}


def resolve_measure(m: dict) -> dict:
    lib_id = m.get("qLibraryId", "")
    if lib_id and lib_id in master_measures:
        return master_measures[lib_id]
    expr  = m.get("qDef", {}).get("qDef", "")
    label = m.get("qDef", {}).get("qLabel", "")
    return {"expression": expr, "label": label}


def extract_object(obj_id: str, obj_type: str, grid: dict, indent: str = "    ") -> dict | None:
    try:
        obj_handle = send("GetObject", app_handle, [obj_id])["qReturn"]["qHandle"]
        raw   = send("GetProperties", obj_handle, [])
        props = raw.get("qProp", raw)
    except Exception as e:
        print(f"{indent}[skip {obj_id}] {e}")
        return {"id": obj_id, "type": obj_type, "error": str(e)}

    viz_type = props.get("visualization", obj_type)
    title    = props.get("title", "")
    subtitle = props.get("subtitle", "")

    dims     = [resolve_dim(d) for d in props.get("qHyperCubeDef", {}).get("qDimensions", [])]
    measures = [resolve_measure(m) for m in props.get("qHyperCubeDef", {}).get("qMeasures", [])]

    filter_fields = []
    if viz_type == "filterpane":
        for fp_child in props.get("qUndoExclude", {}).get("qUndoExcludeList", {}).get("single", []):
            field = fp_child.get("qListObjectDef", {}).get("qDef", {}).get("qFieldDefs", [""])
            lib   = fp_child.get("qLibraryId", "")
            if lib and lib in master_dims:
                filter_fields.append(master_dims[lib]["field"])
            elif field:
                filter_fields.append(field[0])

    obj_out = {
        "id": obj_id, "type": viz_type, "title": title, "subtitle": subtitle,
        "layout": grid, "dimensions": dims, "measures": measures,
    }
    if filter_fields:
        obj_out["filter_fields"] = filter_fields

    # recurse into layout containers
    if viz_type == "sn-layout-container":
        container_props = send("GetProperties", obj_handle, []).get("qProp", {})
        child_grid = {
            c["name"]: {"col": c.get("col", 0), "row": c.get("row", 0),
                        "colspan": c.get("colspan", 1), "rowspan": c.get("rowspan", 1)}
            for c in container_props.get("cells", [])
        }
        child_layout = send("GetLayout", obj_handle, [])["qLayout"]
        child_items  = child_layout.get("qChildList", {}).get("qItems", [])
        children = []
        for child in child_items:
            c_id   = child.get("qInfo", {}).get("qId", "")
            c_type = child.get("qInfo", {}).get("qType", "")
            c_grid = child_grid.get(c_id, {"col": 0, "row": 0, "colspan": 1, "rowspan": 1})
            result = extract_object(c_id, c_type, c_grid, indent + "  ")
            if result:
                children.append(result)
        if children:
            obj_out["children"] = children
            print(f"{indent}[{viz_type}] '{title}' — {len(children)} children")
        return obj_out

    print(f"{indent}[{viz_type}] '{title}' — {len(dims)} dim, {len(measures)} meas — "
          f"grid({grid['col']},{grid['row']},{grid['colspan']}x{grid['rowspan']})")
    return obj_out


# --- list sheets ---
all_infos = send("GetAllInfos", app_handle, [])["qInfos"]
sheet_infos = [o for o in all_infos if o["qType"] == "sheet"]
print(f"Sheets found: {len(sheet_infos)}")

sheets = []

for sheet_info in sheet_infos:
    sheet_id = sheet_info["qId"]
    sheet_handle = send("GetObject", app_handle, [sheet_id])["qReturn"]["qHandle"]

    sheet_props = send("GetProperties", sheet_handle, []).get("qProp", {})
    cells_grid = {
        c["name"]: {"col": c.get("col", 0), "row": c.get("row", 0),
                    "colspan": c.get("colspan", 1), "rowspan": c.get("rowspan", 1)}
        for c in sheet_props.get("cells", [])
    }

    layout = send("GetLayout", sheet_handle, [])["qLayout"]
    sheet_title = layout.get("qMeta", {}).get("title", sheet_id)
    child_items = layout.get("qChildList", {}).get("qItems", [])

    print(f"\n  Sheet: '{sheet_title}' — {len(child_items)} top-level objects")

    objects = []
    for cell in child_items:
        obj_id   = cell.get("qInfo", {}).get("qId", "")
        obj_type = cell.get("qInfo", {}).get("qType", "")
        grid     = cells_grid.get(obj_id, {"col": 0, "row": 0, "colspan": 1, "rowspan": 1})
        result   = extract_object(obj_id, obj_type, grid)
        if result:
            objects.append(result)

    sheets.append({"id": sheet_id, "title": sheet_title, "objects": objects})

out = {"metadata": {"app_name": QVF_NAME}, "sheets": sheets}
out_path = OUTPUT_DIR / "visual_extraction.json"
out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nSaved {len(sheets)} sheets → {out_path}")
ws.close()
