import websocket
import json
ws = websocket.create_connection("ws://localhost:4848/app/")
ws.send(json.dumps({
    "jsonrpc": "2.0",
        "id": 1,
            "method": "OpenDoc",
                "handle": -1,
                    "params": ["DFIN - #13 - SILVER - V1.5 - P.2 Engagement.qvf"]
                    }))
print(ws.recv())
{"jsonrpc":"2.0","method":"OnConnected","params":{"qSessionState":"SESSION_CREATED"}}
resp = json.loads(ws.recv())

app_handle = resp["result"]["qReturn"]["qHandle"]
print("App handle:", app_handle)
App handle: 1
ws.send(json.dumps({
    "jsonrpc": "2.0",
    "id": 2,
    "method": "GetScript",
    "handle": 1,
    "params": []
    }))

script_resp = json.loads(ws.recv())

print(script_resp["result"]["qScript"])

ws.send(json.dumps({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "GetVariableList",
    "handle": 1,
    "params": {}
}))

resp = json.loads(ws.recv())

for v in resp["result"]["qVariableItems"]:
    print(v["qName"], "=", v.get("qDefinition"))