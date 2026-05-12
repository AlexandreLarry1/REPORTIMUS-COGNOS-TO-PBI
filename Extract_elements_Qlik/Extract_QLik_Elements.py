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

# Open document
ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "OpenDoc", "handle": -1, "params": [QVF_NAME]}))
ws.recv()  # OnConnected notification
resp = json.loads(ws.recv())
app_handle = resp["result"]["qReturn"]["qHandle"]
print(f"App handle: {app_handle}")

# Get script
ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "GetScript", "handle": app_handle, "params": []}))
script_resp = json.loads(ws.recv())
script = script_resp["result"]["qScript"]

# Get variables (GetVariableList returns 0 results — use CreateSessionObject + GetLayout)
ws.send(json.dumps({
    "jsonrpc": "2.0", "id": 3,
    "method": "CreateSessionObject",
    "handle": app_handle,
    "params": [{"qInfo": {"qType": "VariableList"}, "qVariableListDef": {"qType": "variable"}}]
}))
resp = json.loads(ws.recv())
var_handle = resp["result"]["qReturn"]["qHandle"]

ws.send(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "GetLayout", "handle": var_handle, "params": []}))
resp = json.loads(ws.recv())
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
