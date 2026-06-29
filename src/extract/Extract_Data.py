"""Export all tables from a Qlik app to CSVs via WebSocket hypercube API."""
import argparse
import csv
import json
import os
import pathlib
import websocket
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent
WS_URL = os.getenv("QLIK_WS_URL", "ws://localhost:4848/app/")
MAX_CELLS = 9000

parser = argparse.ArgumentParser()
parser.add_argument("--app", default=os.getenv("QLIK_QVF_NAME", ""), help="QVF name (e.g. 'Asset Management.qvf')")
args = parser.parse_args()
QVF_NAME = args.app

EXAMPLE_NAME = os.getenv("EXAMPLE_NAME", "")
OUT_DIR = ROOT / "examples" / EXAMPLE_NAME / "input"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ws = websocket.create_connection(WS_URL)
_req_id = 0

def send(method, handle, params):
    global _req_id
    _req_id += 1
    ws.send(json.dumps({"jsonrpc": "2.0", "id": _req_id, "method": method, "handle": handle, "params": params}))
    return _req_id

def recv(expected_id):
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == expected_id:
            if "error" in msg:
                raise RuntimeError(f"Qlik error {msg['error']}")
            return msg

rid = send("GetDocList", -1, [])
docs = recv(rid)["result"]["qDocList"]
match = next((d for d in docs if d.get("qDocName") == QVF_NAME), None)
if not match:
    available = [d.get("qDocName") for d in docs]
    raise RuntimeError(f"App '{QVF_NAME}' not found. Available: {available}")

rid = send("OpenDoc", -1, [match["qDocId"]])
app_handle = recv(rid)["result"]["qReturn"]["qHandle"]
print(f"Opened '{QVF_NAME}' (handle={app_handle})")

rid = send("GetTablesAndKeys", app_handle, [
    {"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 0, True, False
])
tables = recv(rid)["result"].get("qtr", [])
print(f"Tables found: {[t['qName'] for t in tables]}")

for table in tables:
    tname = table["qName"]
    fields = [f["qName"] for f in table.get("qFields", [])]
    if not fields:
        print(f"  [{tname}] no fields, skipping")
        continue

    print(f"  Exporting {tname} ({len(fields)} fields)…")

    hc_def = {
        "qInfo": {"qType": "DataExport"},
        "qHyperCubeDef": {
            "qDimensions": [
                {"qDef": {"qFieldDefs": [f], "qFieldLabels": [f]}}
                for f in fields
            ],
            "qMeasures": [],
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": 1, "qWidth": len(fields)}],
            "qSuppressZero": False,
            "qSuppressMissing": False,
        }
    }
    rid = send("CreateSessionObject", app_handle, [hc_def])
    hc_handle = recv(rid)["result"]["qReturn"]["qHandle"]

    rid = send("GetLayout", hc_handle, [])
    layout = recv(rid)["result"]["qLayout"]
    n_rows = layout["qHyperCube"]["qSize"]["qcy"]
    n_cols = len(fields)
    print(f"    {n_rows} rows × {n_cols} cols")

    page_size = max(1, MAX_CELLS // n_cols)
    rows_data = []
    top = 0
    while top < n_rows:
        height = min(page_size, n_rows - top)
        rid = send("GetHyperCubeData", hc_handle, [
            "/qHyperCubeDef",
            [{"qTop": top, "qLeft": 0, "qHeight": height, "qWidth": n_cols}]
        ])
        pages = recv(rid)["result"]["qDataPages"]
        for page in pages:
            for row in page["qMatrix"]:
                rows_data.append([cell.get("qText", "") for cell in row])
        top += height
        print(f"    …{top}/{n_rows}")

    out_path = OUT_DIR / f"{tname}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        writer.writerows(rows_data)
    print(f"    → examples/{EXAMPLE_NAME}/input/{tname}.csv")

ws.close()
print("Done.")
