"""A pretend merchant app that receives events from Relay.

    python demo/merchant.py                      # listens on :9000
    curl -X POST localhost:9000/toggle           # simulate an outage (503s)
    curl localhost:9000/stats                    # what it has received

It counts duplicates by Relay-Event-Id, so you can see Relay never sends one twice.
"""

from collections import Counter

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Demo merchant")
state = {"up": True}
seen: Counter[str] = Counter()
order: list[str] = []


@app.post("/events")
async def receive(request: Request):
    if not state["up"]:
        print("DOWN  -> 503")
        return JSONResponse({"error": "merchant is down"}, status_code=503)
    event_id = request.headers.get("Relay-Event-Id", "?")
    body = await request.json()
    seen[event_id] += 1
    duplicate = seen[event_id] > 1
    if not duplicate:
        order.append(event_id)
    print(f"got {event_id} {body.get('type')}{'  DUPLICATE!' if duplicate else ''}")
    return {"ok": True, "duplicate": duplicate}


@app.post("/toggle")
def toggle():
    state["up"] = not state["up"]
    print(f"merchant is now {'UP' if state['up'] else 'DOWN'}")
    return {"up": state["up"]}


@app.get("/stats")
def stats():
    return {
        "up": state["up"],
        "unique": len(seen),
        "duplicates": sum(n - 1 for n in seen.values()),
        "order": order,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
