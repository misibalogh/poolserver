# Szerver.py – FastAPI + WebSocket lobby / invite / room (no chat), in-memory
import os, json, time, asyncio, secrets
from typing import Dict, Set, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Pool MP Lobby Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---- In-memory state ----
users: Dict[str, dict] = {}   # userId -> {name, ws, last, status, roomId, resume}
rooms: Dict[str, dict] = {}   # roomId -> {players:[u1,u2], created, state?}
invites: Dict[str, dict] = {} # inviteId -> {from, to, created, expires, status}

PRESENCE_EVERY = 5.0
INVITE_TTL = 30.0
IDLE_TTL = 120.0

def now() -> float: return time.time()

async def safe_send(ws: WebSocket, obj: dict):
    try:
        await ws.send_text(json.dumps(obj))
    except Exception:
        pass

async def broadcast_userlist():
    lst = [{"id": uid, "name": u["name"], "status": u.get("status","lobby"), "roomId": u.get("roomId")} 
           for uid,u in users.items() if u.get("ws")]
    msg = {"t":"presence", "users": lst}
    for u in list(users.values()):
        if u.get("ws"):
            await safe_send(u["ws"], msg)

def cleanup():
    # expire invites
    nowt = now()
    for inv_id, inv in list(invites.items()):
        if inv["status"] == "sent" and nowt > inv["expires"]:
            inv["status"] = "expired"
            # notify both if online
            for uid in (inv["from"], inv["to"]):
                u = users.get(uid)
                if u and u.get("ws"):
                    asyncio.create_task(safe_send(u["ws"], {"t":"invite_update","inviteId":inv_id,"status":"expired"}))
            invites.pop(inv_id, None)
    # remove empty rooms
    for rid, r in list(rooms.items()):
        ps = r["players"]
        if all((users.get(uid, {}).get("roomId") != rid) for uid in ps):
            rooms.pop(rid, None)

async def handle_hello(ws: WebSocket, data: dict):
    name = (data.get("name") or "").strip()[:24] or "Játékos"
    resume = (data.get("resumeToken") or "").strip()
    # try resume
    foundId: Optional[str] = None
    if resume:
        for uid,u in users.items():
            if u.get("resume")==resume:
                foundId = uid
                if u.get("ws"):  # kick old
                    try: await u["ws"].close()
                    except: pass
                u["ws"]=ws; u["last"]=now(); u["status"]="lobby"; u.pop("roomId", None)
                break
    if not foundId:
        uid = secrets.token_hex(4)
        users[uid] = {"name": name, "ws": ws, "last": now(), "status": "lobby", "resume": secrets.token_hex(8)}
        foundId = uid
    u = users[foundId]
    # welcome
    await safe_send(ws, {"t":"welcome","userId":foundId,"name":u["name"],"resumeToken":u["resume"]})
    await broadcast_userlist()

async def handle_invite(ws: WebSocket, sender_id: str, data: dict):
    to = data.get("to")
    if not to or to == sender_id or to not in users: 
        return await safe_send(ws, {"t":"error","code":"bad_invitee","msg":"Érvénytelen cél."})
    inv_id = secrets.token_hex(5)
    invites[inv_id] = {"from": sender_id, "to": to, "created": now(), "expires": now()+INVITE_TTL, "status":"sent"}
    # notify both
    for uid in (sender_id, to):
        u = users.get(uid)
        if u and u.get("ws"):
            await safe_send(u["ws"], {"t":"invite_update","inviteId":inv_id,"from":sender_id,"to":to,"status":"sent","ttl":INVITE_TTL})

async def handle_invite_answer(ws: WebSocket, who_id: str, data: dict, accept: bool):
    inv_id = data.get("inviteId")
    inv = invites.get(inv_id)
    if not inv or inv["status"]!="sent": 
        return await safe_send(ws, {"t":"error","code":"invite_missing","msg":"Meghívás nem található."})
    if inv["to"] != who_id:
        return await safe_send(ws, {"t":"error","code":"not_receiver","msg":"Nem te vagy a címzett."})
    inv["status"] = "accepted" if accept else "declined"
    # notify both
    for uid in (inv["from"], inv["to"]):
        u = users.get(uid)
        if u and u.get("ws"):
            await safe_send(u["ws"], {"t":"invite_update","inviteId":inv_id,"status":inv["status"]})
    if not accept:
        invites.pop(inv_id, None)
        return
    # create room
    roomId = secrets.token_hex(4)
    rooms[roomId] = {"players":[inv["from"], inv["to"]], "created": now()}
    for i, uid in enumerate(rooms[roomId]["players"]):
        u = users.get(uid)
        if u:
            u["status"]="in_room"; u["roomId"]=roomId
            if u.get("ws"):
                await safe_send(u["ws"], {"t":"room_start","roomId":roomId,"youAre":"host" if i==0 else "guest","players":[{"id":p,"name":users[p]["name"]} for p in rooms[roomId]["players"]]})
    invites.pop(inv_id, None)
    await broadcast_userlist()

async def handle_room_msg(sender_id: str, data: dict):
    rid = data.get("roomId")
    if not rid or rid not in rooms or sender_id not in rooms[rid]["players"]:
        return
    # relay allowed keys
    t = data.get("t")
    if t in ("shoot","state","leave_room"):
        for uid in rooms[rid]["players"]:
            u = users.get(uid)
            if u and u.get("ws"):
                await safe_send(u["ws"], data)

async def ws_main(ws: WebSocket):
    await ws.accept()
    user_id: Optional[str] = None
    try:
        # main loop
        while True:
            cleanup()
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=25.0)
            except asyncio.TimeoutError:
                await safe_send(ws, {"t":"ping"}); continue
            data = {}
            try: data = json.loads(raw)
            except: 
                await safe_send(ws, {"t":"error","code":"json","msg":"Rossz JSON."}); continue
            t = data.get("t")
            # find user_id if exists
            for uid,u in users.items():
                if u.get("ws") is ws:
                    user_id = uid; break
            if t == "hello":
                await handle_hello(ws, data)
            elif not user_id:
                await safe_send(ws, {"t":"error","code":"no_hello","msg":"Előbb hello."})
            elif t == "invite":
                await handle_invite(ws, user_id, data)
            elif t == "accept_invite":
                await handle_invite_answer(ws, user_id, data, True)
            elif t == "decline_invite":
                await handle_invite_answer(ws, user_id, data, False)
            elif t in ("shoot","state","leave_room"):
                await handle_room_msg(user_id, data)
            elif t == "pong":
                pass
    except WebSocketDisconnect:
        pass
    finally:
        # detach user
        for uid,u in list(users.items()):
            if u.get("ws") is ws:
                u["ws"]=None; u["last"]=now()
                # keep user for resume; if in room, free status
                rid = u.pop("roomId", None)
                u["status"]="lobby"
        cleanup()
        await broadcast_userlist()

@app.get("/")
def root(): return {"ok": True, "msg": "Pool MP server running."}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket): await ws_main(ws)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
