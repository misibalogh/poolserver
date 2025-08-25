# Szerver.py — FastAPI + WebSocket lobby/invite/room + 8-ball bíró (authoritative rules)
# Koyeb-hez Procfile: web: uvicorn Szerver:app --host 0.0.0.0 --port 8000

import os, json, time, asyncio, secrets, math, random
from typing import Dict, Optional, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Pool MP Lobby Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---- Állapot ----
users: Dict[str, dict] = {}   # userId -> {name, ws, last, status, roomId, resume}
rooms: Dict[str, dict] = {}   # roomId -> roomState
invites: Dict[str, dict] = {} # inviteId -> {from, to, created, expires, status}

INVITE_TTL = 30.0

SOLIDS  = set(range(1,8))
STRIPES = set(range(9,16))

def now() -> float: return time.time()

async def safe_send(ws: WebSocket, obj: dict):
    try:
        await ws.send_text(json.dumps(obj))
    except Exception:
        pass

def presence_payload():
    return {"t":"presence", "users":[
        {"id":uid, "name":u["name"], "status":u.get("status","lobby"), "roomId":u.get("roomId")}
        for uid,u in users.items() if u.get("ws")
    ]}

async def broadcast_userlist():
    msg = presence_payload()
    for u in list(users.values()):
        if u.get("ws"): await safe_send(u["ws"], msg)

def cleanup():
    tnow = now()
    # invites lejárat
    for inv_id, inv in list(invites.items()):
        if inv["status"]=="sent" and tnow > inv["expires"]:
            inv["status"]="expired"
            for uid in (inv["from"], inv["to"]):
                u = users.get(uid)
                if u and u.get("ws"):
                    asyncio.create_task(safe_send(u["ws"], {"t":"invite_update","inviteId":inv_id,"status":"expired"}))
            invites.pop(inv_id, None)
    # üres szobák törlése
    for rid, r in list(rooms.items()):
        players = r["players"]
        if all(users.get(uid,{}).get("roomId")!=rid for uid in players):
            rooms.pop(rid, None)

@app.on_event("startup")
async def periodic_presence():
    async def loop():
        while True:
            await asyncio.sleep(3)
            await broadcast_userlist()
    asyncio.create_task(loop())

# ------------- JÁTÉK / SZABÁLYOK (a kliens fizikát számol, a szerver a bíró) ----------------

def num_to_group(n:int):
    if n in SOLIDS: return "SOLID"
    if n in STRIPES: return "STRIPE"
    if n==8: return "EIGHT"
    return None

TABLE = {
    "LW": 1280, "LH": 720,
    "MARGIN": 40, "CUSHION": 34
}
TABLE["INNER_W"] = TABLE["LW"] - 2*(TABLE["MARGIN"]+TABLE["CUSHION"])
TABLE["INNER_H"] = TABLE["LH"] - 2*(TABLE["MARGIN"]+TABLE["CUSHION"])
TABLE["X"] = TABLE["MARGIN"] + TABLE["CUSHION"]
TABLE["Y"] = TABLE["MARGIN"] + TABLE["CUSHION"]
RADIUS = 14
POCKET_R = int(RADIUS * 2.6)
POCKET_INSET = max(4, int(RADIUS * 0.35))

def rack_positions():
    start_x = TABLE["X"] + TABLE["INNER_W"]*0.65
    dx = RADIUS*2*math.cos(math.radians(30))
    pos=[]; rows=5
    for r in range(rows):
        for i in range(r+1):
            x = start_x + dx*r
            y = TABLE["Y"] + TABLE["INNER_H"]/2 - r*RADIUS + i*2*RADIUS
            pos.append((x,y))
    random.shuffle(pos)
    return pos

def make_new_balls():
    # fehér
    balls = {"0":{"x":TABLE["X"]+TABLE["INNER_W"]*0.22, "y":TABLE["Y"]+TABLE["INNER_H"]/2, "alive":True}}
    # 1..15 + 8 középre
    spots = rack_positions()
    nums = list(range(1,16)); random.shuffle(nums)
    if 8 in nums:
        i8 = nums.index(8); mid = len(nums)//2
        nums[i8], nums[mid] = nums[mid], nums[i8]
    for i,(x,y) in enumerate(spots[:15]):
        n = nums[i]
        balls[str(n)] = {"x":x, "y":y, "alive":True}
    return balls

def clamp(v,a,b): 
    return a if v<a else (b if v>b else v)

def valid_cue_place(x:float, y:float, balls:Dict[str,dict]) -> bool:
    # asztalon belül és nem ér másik labdát
    minx = TABLE["X"] + RADIUS
    maxx = TABLE["X"] + TABLE["INNER_W"] - RADIUS
    miny = TABLE["Y"] + RADIUS
    maxy = TABLE["Y"] + TABLE["INNER_H"] - RADIUS
    if not (minx <= x <= maxx and miny <= y <= maxy): 
        return False
    for k,v in balls.items():
        if k=="0" or (not v["alive"]): continue
        if (v["x"]-x)**2 + (v["y"]-y)**2 < (2*RADIUS+1)**2:
            return False
    return True

def evaluate_shot(room:dict, shooter:str, shot:dict) -> dict:
    """
    shot = { first_hit: int|null, any_rail: bool, potted: [ints], balls: {num:{x,y,alive}} }
    returns: dict with updated room fields and message
    """
    groups = room["groups"]
    opponent = room["players"][1] if room["players"][0]==shooter else room["players"][0]

    potted: List[int] = shot.get("potted") or []
    first_hit = shot.get("first_hit", None)
    any_rail = bool(shot.get("any_rail", False))
    balls = shot.get("balls") or room["balls"]  # fallback
    # némi szanálás: floatok és alive bool
    for k,v in list(balls.items()):
        try:
            balls[k] = {"x": float(v["x"]), "y": float(v["y"]), "alive": bool(v["alive"])}
        except Exception:
            pass

    foul = False; reasons = []
    # scratch?
    if 0 in potted:
        foul = True; reasons.append("Scratch (fehér beesett)")
        balls["0"]["alive"] = True  # a fehéret nem tartjuk kint, később ball-in-hand lerakja a másik

    # első érintés
    if first_hit is None:
        foul = True; reasons.append("Nem ért labdát")
    else:
        g = groups.get(shooter)
        if g is not None:
            if g=="SOLID" and (first_hit in STRIPES or first_hit==8):
                foul=True; reasons.append("Első érintés nem saját (teli kellene)")
            if g=="STRIPE" and (first_hit in SOLIDS or first_hit==8):
                foul=True; reasons.append("Első érintés nem saját (csíkos kellene)")
        else:
            if first_hit==8:
                foul=True; reasons.append("Első érintés 8-as kiosztás előtt")

    # ha nincs zsebelés ÉS nincs falérintés -> foul
    potted_non8 = [n for n in potted if n not in (0,8)]
    if (not potted) and (not any_rail):
        foul = True; reasons.append("Nincs zsebelés és falérintés sem")

    # 8-as esete
    if 8 in potted:
        myg = groups.get(shooter)
        alive_nums = {int(k) for k,v in balls.items() if v["alive"]}
        my_need = (SOLIDS if myg=="SOLID" else STRIPES) if myg else set()
        left = my_need & alive_nums
        if myg is None or (left and len(left)>0) or foul:
            # vesztes
            room["winner"] = opponent
            room["msg"] = f"{room['names'][shooter]} szabálytalanul zsebelte a 8-ast → {room['names'][opponent]} nyert!"
            room["turn"] = None
            room["balls"] = balls
            return room
        else:
            # győztes
            room["winner"] = shooter
            room["msg"] = f"8-as szabályosan zsebelve → {room['names'][shooter]} nyert!"
            room["turn"] = None
            room["balls"] = balls
            return room

    # kiosztás eldöntése (ha még nincs)
    shooter_g = groups.get(shooter)
    opp_g = groups.get(opponent)
    potted_solid = [n for n in potted_non8 if n in SOLIDS]
    potted_stripe= [n for n in potted_non8 if n in STRIPES]
    if shooter_g is None and opp_g is None:
        if potted_solid and not potted_stripe:
            room["groups"][shooter] = "SOLID"; room["groups"][opponent]="STRIPE"
            room["msg"] = f"Csoport kiosztás: {room['names'][shooter]}=Teli, {room['names'][opponent]}=Csíkos."
        elif potted_stripe and not potted_solid:
            room["groups"][shooter] = "STRIPE"; room["groups"][opponent]="SOLID"
            room["msg"] = f"Csoport kiosztás: {room['names'][shooter]}=Csíkos, {room['names'][opponent]}=Teli."
        elif potted_non8:
            room["msg"] = "Mindkét típus esett kiosztás előtt → még nincs kiosztás."
        else:
            room["msg"] = ""

    # körváltás / ball-in-hand
    if foul:
        room["ball_in_hand"][opponent] = True
        room["turn"] = opponent
        room["msg"] = ("FOUL: " + "; ".join(reasons) +
                       f" — {room['names'][opponent]} jön (ball-in-hand).")
    else:
        cont = False
        myg = room["groups"].get(shooter)
        if myg is None:
            cont = len(potted_non8)>0
        else:
            need = SOLIDS if myg=="SOLID" else STRIPES
            cont = any(n in need for n in potted_non8)
        if cont:
            room["msg"] = f"{room['names'][shooter]} zsebelt → újra ő jön."
            room["turn"] = shooter
        else:
            room["turn"] = opponent
            room["msg"] = f"Nem esett saját → {room['names'][opponent]} jön."

    room["balls"] = balls
    return room

def start_new_game(room:dict):
    room["balls"] = make_new_balls()
    room["turn"] = room["players"][0]   # a meghívó kezd
    room["groups"] = {room["players"][0]: None, room["players"][1]: None}
    room["ball_in_hand"] = {room["players"][0]: False, room["players"][1]: False}
    room["winner"] = None
    room["shots"] = {room["players"][0]:0, room["players"][1]:0}
    room["msg"] = f"Kezdés: {room['names'][room['turn']]} tör."
    room["started"] = True

async def broadcast_room(room_id:str):
    room = rooms.get(room_id)
    if not room: return
    payload = {
        "t":"game_state",
        "roomId": room_id,
        "players":[{"id":p,"name":room["names"][p]} for p in room["players"]],
        "turn": room["turn"],
        "groups": room["groups"],
        "ball_in_hand": room["ball_in_hand"],
        "winner": room["winner"],
        "shots": room["shots"],
        "msg": room.get("msg",""),
        "balls": room["balls"],
    }
    for uid in room["players"]:
        u = users.get(uid)
        if u and u.get("ws"):
            await safe_send(u["ws"], payload)

# ------------- Üzenetkezelés ----------------

async def handle_hello(ws: WebSocket, data: dict):
    name = (data.get("name") or "").strip()[:24] or "Játékos"
    resume = (data.get("resumeToken") or "").strip()
    found_id: Optional[str] = None
    if resume:
        for uid,u in users.items():
            if u.get("resume")==resume:
                found_id = uid
                if u.get("ws"):
                    try: await u["ws"].close()
                    except: pass
                u["ws"]=ws; u["last"]=now(); u["status"]="lobby"; u.pop("roomId", None)
                break
    if not found_id:
        uid = secrets.token_hex(4)
        users[uid] = {"name": name, "ws": ws, "last": now(), "status": "lobby", "resume": secrets.token_hex(8)}
        found_id = uid
    u = users[found_id]
    await safe_send(ws, {"t":"welcome","userId":found_id,"name":u["name"],"resumeToken":u["resume"]})
    await safe_send(ws, presence_payload()) # azonnali lista a belépőnek
    await broadcast_userlist()

async def handle_invite(ws: WebSocket, sender_id: str, data: dict):
    to = data.get("to")
    if not to or to == sender_id or to not in users:
        return await safe_send(ws, {"t":"error","code":"bad_invitee","msg":"Érvénytelen cél."})
    inv_id = secrets.token_hex(5)
    invites[inv_id] = {"from": sender_id, "to": to, "created": now(), "expires": now()+INVITE_TTL, "status":"sent"}
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
    for uid in (inv["from"], inv["to"]):
        u = users.get(uid)
        if u and u.get("ws"):
            await safe_send(u["ws"], {"t":"invite_update","inviteId":inv_id,"status":inv["status"]})

    if not accept:
        invites.pop(inv_id, None)
        return

    # create room + start game
    room_id = secrets.token_hex(4)
    p1, p2 = inv["from"], inv["to"]
    rooms[room_id] = {
        "players":[p1,p2],
        "names": {p1: users[p1]["name"], p2: users[p2]["name"]},
        "created": now(),
    }
    for i, uid in enumerate([p1,p2]):
        u = users.get(uid); 
        if u:
            u["status"]="in_room"; u["roomId"]=room_id
            if u.get("ws"):
                await safe_send(u["ws"], {
                    "t":"room_start","roomId":room_id,"youAre":"host" if i==0 else "guest",
                    "players":[{"id":p,"name":users[p]["name"]} for p in [p1,p2]]
                })
    invites.pop(inv_id, None)
    start_new_game(rooms[room_id])
    await broadcast_userlist()
    await broadcast_room(room_id)

async def handle_game_msg(sender_id: str, data: dict):
    rid = data.get("roomId")
    if not rid or rid not in rooms: return
    room = rooms[rid]
    if sender_id not in room["players"]: return

    t = data.get("t")
    if t == "get_users":
        # handled elsewhere; ignore here
        return
    if t == "place_cue":
        # ball-in-hand lerakás
        x = float(data.get("x", 0)); y = float(data.get("y", 0))
        if room["ball_in_hand"].get(sender_id) and valid_cue_place(x,y, room["balls"]):
            room["balls"]["0"] = {"x":x, "y":y, "alive":True}
            room["ball_in_hand"][sender_id] = False
            room["msg"] = f"{room['names'][sender_id]} lerakta a fehéret (ball-in-hand)."
            await broadcast_room(rid)
        else:
            u = users.get(sender_id)
            if u and u.get("ws"):
                await safe_send(u["ws"], {"t":"error","code":"place_cue","msg":"Nem tehető ide (kívül/ütközés/nem jogosult)."})
        return

    if t == "shot_end":
        # csak az lőhet, akinek köre van
        if room.get("turn") != sender_id or room.get("winner"):
            return
        room["shots"][sender_id] = room["shots"].get(sender_id,0)+1
        # szabályértékelés
        shot = {
            "first_hit": data.get("first_hit"),
            "any_rail": bool(data.get("any_rail", False)),
            "potted": data.get("potted") or [],
            "balls": data.get("balls") or room["balls"],
        }
        evaluate_shot(room, sender_id, shot)
        await broadcast_room(rid)
        return

    if t == "leave_room":
        # vissza lobbyba
        for uid in room["players"]:
            u = users.get(uid)
            if u:
                u["status"]="lobby"; u.pop("roomId", None)
                if u.get("ws"): await safe_send(u["ws"], {"t":"left_room","roomId":rid})
        rooms.pop(rid, None)
        await broadcast_userlist()
        return

async def ws_main(ws: WebSocket):
    await ws.accept()
    user_id: Optional[str] = None
    try:
        while True:
            cleanup()
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=25.0)
            except asyncio.TimeoutError:
                await safe_send(ws, {"t":"ping"}); 
                continue
            try:
                data = json.loads(raw)
            except:
                await safe_send(ws, {"t":"error","code":"json","msg":"Rossz JSON."}); 
                continue

            t = data.get("t")
            # melyik userhez tartozik ez a ws?
            for uid,u in users.items():
                if u.get("ws") is ws:
                    user_id = uid; break

            if t == "hello":
                await handle_hello(ws, data)
            elif t == "get_users":
                await safe_send(ws, presence_payload())
            elif not user_id:
                await safe_send(ws, {"t":"error","code":"no_hello","msg":"Előbb hello."})
            elif t == "invite":
                await handle_invite(ws, user_id, data)
            elif t == "accept_invite":
                await handle_invite_answer(ws, user_id, data, True)
            elif t == "decline_invite":
                await handle_invite_answer(ws, user_id, data, False)
            elif t in ("place_cue","shot_end","leave_room"):
                await handle_game_msg(user_id, data)
            elif t == "pong":
                pass
    except WebSocketDisconnect:
        pass
    finally:
        # lecsatlakozás
        for uid,u in list(users.items()):
            if u.get("ws") is ws:
                u["ws"]=None; u["last"]=now(); u.pop("roomId", None); u["status"]="lobby"
        cleanup()
        await broadcast_userlist()

@app.get("/")
def root(): 
    return {"ok": True, "msg": "Pool MP server running."}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket): 
    await ws_main(ws)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
