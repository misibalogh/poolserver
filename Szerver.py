# Szerver.py — FastAPI + WebSocket szobákkal (javított indítás)
import os, json, asyncio, time
from typing import Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Pool MP Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

rooms: Dict[str, Set[WebSocket]] = {}

async def broadcast(room: str, payload: dict):
    """Üzenet küldése a szoba összes kliensének; hibás socketek takarítása."""
    stale = []
    for ws in list(rooms.get(room, set())):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            stale.append(ws)
    for ws in stale:
        rooms[room].discard(ws)
    if room in rooms and not rooms[room]:
        rooms.pop(room)

@app.get("/")
def root():
    return {"ok": True, "msg": "Pool MP server running."}

@app.get("/health")
def health():
    return {"ok": True, "t": time.time()}

@app.websocket("/ws/{room}")
async def ws_room(ws: WebSocket, room: str):
    await ws.accept()
    rooms.setdefault(room, set()).add(ws)
    await broadcast(room, {"type": "system", "msg": "joined"})
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=25.0)
                msg = json.loads(raw)
            except asyncio.TimeoutError:
                # keepalive – segít, hogy free hoszton ne szakadjon le
                await ws.send_text(json.dumps({"type": "ping"}))
                continue

            t = msg.get("type")
            if t == "shoot":
                await broadcast(room, {
                    "type": "shoot",
                    "angle": msg.get("angle"),
                    "power": msg.get("power")
                })
            elif t == "state":
                await broadcast(room, {
                    "type": "state",
                    "balls": msg.get("balls")
                })
            elif t == "join":
                await broadcast(room, {"type": "system", "msg": "joined"})
            elif t == "ping":
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        s = rooms.get(room)
        if s:
            s.discard(ws)
            if not s:
                rooms.pop(room, None)
        await broadcast(room, {"type": "system", "msg": "left"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # FONTOS: közvetlenül az app objektumot adjuk át → bármilyen fájlnévvel működik
    uvicorn.run(app, host="0.0.0.0", port=port)
