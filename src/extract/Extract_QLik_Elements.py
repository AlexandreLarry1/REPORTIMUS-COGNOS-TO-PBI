"""Extract script and variables from a Qlik app via WebSocket API."""
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
parser.add_argument("--app", default=os.getenv("QLIK_QVF_NAME", ""), help="QVF name (e.g. 'Asset Management.qvf')")
args = parser.parse_args()
QVF_NAME = args.app

EXAMPLE_NAME = os.getenv("EXAMPLE_NAME", "")
INTER_DIR = ROOT / "examples" / EXAMPLE_NAME / "intermediate"
INTER_DIR.mkdir(parents=True, exist_ok=True)

ws = websocket.create_connection(WS_URL)

def recv_result(expected_id):
    while True:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == expected_id:
            if "error" in msg:
                raise RuntimeError(f"Qlik error (id={expected_id}): {msg['error']}")
            return msg
        print(f"  [skip notification] method={msg.get('method','?')}")

ws.send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "GetDocList", "handle": -1, "params": []}))
doc_list = recv_result(0)
docs = doc_list["result"]["qDocList"]

match = next((d for d in docs if d.get("qDocName") == QVF_NAME), None)
if match is None:
    available = [d.get("qDocName") for d in docs]
    raise RuntimeError(f"App '{QVF_NAME}' not found. Available: {available}")
doc_id = match["qDocId"]

ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "OpenDoc", "handle": -1, "params": [doc_id]}))
resp = recv_result(1)
app_handle = resp["result"]["qReturn"]["qHandle"]
print(f"App handle: {app_handle}")

ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "GetScript", "handle": app_handle, "params": []}))
script_resp = recv_result(2)
script = script_resp["result"]["qScript"]

ws.send(json.dumps({
    "jsonrpc": "2.0", "id": 3,
    "method": "CreateSessionObject",
    "handle": app_handle,
    "params": [{"qInfo": {"qType": "VariableList"}, "qVariableListDef": {"qType": "variable"}}]
}))
resp = recv_result(3)
var_handle = resp["result"]["qReturn"]["qHandle"]

ws.send(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "GetLayout", "handle": var_handle, "params": []}))
resp = recv_result(4)
variables = [
    {"name": v["qName"], "definition": v.get("qDefinition", "")}
    for v in resp["result"]["qLayout"]["qVariableList"]["qItems"]
]

result = {"script": script, "variables": variables}
out_path = INTER_DIR / "extraction.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"Saved {len(variables)} variables + script → {out_path}")
ws.close()
