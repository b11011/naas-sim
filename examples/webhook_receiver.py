"""Tiny webhook receiver: prints every event the simulator delivers.

Run:
    uvicorn examples.webhook_receiver:app --port 9000

Register it with the simulator (needs a bearer token):
    curl -X POST http://localhost:8080/Customer/v3/Ordering/notifications \
         -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
         -d '{"callback": "http://localhost:9000/events"}'
"""
import json

from fastapi import FastAPI, Request

app = FastAPI(title="NaaS lab webhook receiver")


@app.post("/events")
async def receive(request: Request):
    event = await request.json()
    print(json.dumps(event, indent=2), flush=True)
    return {"received": True}
