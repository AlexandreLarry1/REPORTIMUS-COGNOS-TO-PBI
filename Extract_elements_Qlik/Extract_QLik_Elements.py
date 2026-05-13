"""Extract script and variables from a Qlik app via WebSocket API."""
import json
import os
import pathlib
import websocket
from dotenv import load_dotenv

load_dotenv()

WS_URL = os.getenv("QLIK_WS_URL", "ws://localhost:4848/app/")
QVF_NAME = os.getenv("QLIK_QVF_NAME", "")
OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

ws = websocket.create_connection(WS_URL)

def recv_result(expected_id):
    """Read messages until we get the response matching expected_id."""
    while True:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == expected_id:
            if "error" in msg:
                raise RuntimeError(f"Qlik error (id={expected_id}): {msg['error']}")
            return msg
        print(f"  [skip notification] method={msg.get('method','?')}")

# List available apps (debug)
ws.send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "GetDocList", "handle": -1, "params": []}))
doc_list = recv_result(0)
docs = doc_list["result"]["qDocList"]
print(f"Available apps ({len(docs)}):")
for d in docs:
    print(f"  - {d.get('qDocName')}  (id={d.get('qDocId')})")

match = next((d for d in docs if d.get("qDocName") == QVF_NAME), None)
if match is None:
    raise RuntimeError(f"App '{QVF_NAME}' not found. Check QLIK_QVF_NAME in .env.")
doc_id = match["qDocId"]

# Open document using full path id
ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "OpenDoc", "handle": -1, "params": [doc_id]}))
resp = recv_result(1)
app_handle = resp["result"]["qReturn"]["qHandle"]
print(f"App handle: {app_handle}")

# Get script
ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "GetScript", "handle": app_handle, "params": []}))
script_resp = recv_result(2)
script = script_resp["result"]["qScript"]

# Get variables (GetVariableList returns 0 results — use CreateSessionObject + GetLayout)
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

# Save output
result = {"script": script, "variables": variables}
out_path = OUTPUT_DIR / "extraction.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"Saved {len(variables)} variables + script → {out_path}")
ws.close()
