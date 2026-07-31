import asyncio
import json
import os
import threading
import time

import requests
import websockets
from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE = os.path.join(BASE, "sessions.json")
STATE_FILE = os.path.join(BASE, "state.json")
GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"

app = Flask(__name__)

hangs = {}
_loop = None
_loop_thread = None
_restored = False


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_loop():
    global _loop, _loop_thread
    if _loop is None:
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
        _loop_thread.start()
    return _loop


def submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, get_loop())


async def _make_event():
    return asyncio.Event()


def call(fn):
    get_loop().call_soon_threadsafe(fn)


def dapi(method, path, token, **kw):
    kw.setdefault("timeout", 15)
    return requests.request(
        method,
        "https://discord.com/api/v9" + path,
        headers={"Authorization": token},
        **kw,
    )


def get_token():
    return (request.headers.get("X-Token") or request.args.get("token") or "").strip()


def require_token():
    token = get_token()
    if token not in load_json(SESSIONS_FILE):
        return None, (jsonify({"ok": False, "error": "Phiên đăng nhập hết hạn, hãy đăng nhập lại"}), 401)
    return token, None


async def hang_voice(token, guild_id, channel_id, stop_event):
    while not stop_event.is_set():
        try:
            async with websockets.connect(GATEWAY, max_size=None, ping_interval=None) as ws:
                hello = json.loads(await ws.recv())
                hb = hello["d"]["heartbeat_interval"] / 1000

                async def heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(hb)
                            await ws.send(json.dumps({"op": 1, "d": None}))
                    except Exception:
                        return

                hb_task = asyncio.create_task(heartbeat())
                try:
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": token,
                            "properties": {"$os": "windows", "$browser": "chrome", "$device": "pc"},
                            "intents": 513,
                        },
                    }))

                    while True:
                        ev = json.loads(await ws.recv())
                        if ev["op"] == 0 and ev["t"] == "READY":
                            break

                    await ws.send(json.dumps({
                        "op": 4,
                        "d": {
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "self_mute": True,
                            "self_deaf": False,
                            "self_video": True,
                        },
                    }))

                    stop_task = asyncio.create_task(stop_event.wait())
                    while True:
                        recv_task = asyncio.create_task(ws.recv())
                        done, _ = await asyncio.wait(
                            {stop_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if stop_task in done:
                            recv_task.cancel()
                            try:
                                await ws.send(json.dumps({
                                    "op": 4,
                                    "d": {
                                        "guild_id": guild_id,
                                        "channel_id": None,
                                        "self_mute": False,
                                        "self_deaf": False,
                                        "self_video": False,
                                    },
                                }))
                            except Exception:
                                pass
                            return
                        recv_task.result()
                finally:
                    hb_task.cancel()
        except Exception:
            if stop_event.is_set():
                return
            await asyncio.sleep(5)


@app.before_request
def restore_hangs():
    global _restored
    if _restored:
        return
    _restored = True
    state = load_json(STATE_FILE)
    for token, info in state.items():
        stop = submit(_make_event()).result()
        hangs[token] = {"stop": stop, "started_at": info.get("started_at", time.time())}
        submit(hang_voice(token, info["guild_id"], info["channel_id"], stop))


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Vui lòng nhập token"}), 400
    try:
        r = dapi("GET", "/users/@me", token)
    except Exception:
        return jsonify({"ok": False, "error": "Không kết nối được Discord API"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": "Token không hợp lệ hoặc đã hết hạn"}), 401
    u = r.json()
    sessions = load_json(SESSIONS_FILE)
    sessions[token] = {
        "id": u["id"],
        "username": u.get("username", "user"),
        "avatar": u.get("avatar"),
    }
    save_json(SESSIONS_FILE, sessions)
    return jsonify({"ok": True, "user": {
        "id": u["id"],
        "username": u.get("username", "user"),
        "avatar": u.get("avatar"),
    }})


@app.route("/api/guilds")
def api_guilds():
    token, err = require_token()
    if err:
        return err
    try:
        r = dapi("GET", "/users/@me/guilds", token)
    except Exception:
        return jsonify({"ok": False, "error": "Lỗi kết nối Discord API"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": "Token đã bị Discord thu hồi"}), 401
    guilds = sorted(r.json(), key=lambda g: g.get("position", 0))
    return jsonify({"ok": True, "guilds": [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")} for g in guilds
    ]})


@app.route("/api/guilds/<gid>/channels")
def api_channels(gid):
    token, err = require_token()
    if err:
        return err
    try:
        r = dapi("GET", f"/guilds/{gid}/channels", token)
    except Exception:
        return jsonify({"ok": False, "error": "Lỗi kết nối Discord API"}), 502
    if r.status_code != 200:
        return jsonify({"ok": False, "error": "Không truy cập được server này"}), 401
    channels = sorted(
        [c for c in r.json() if c.get("type") == 2],
        key=lambda c: c.get("position", 0),
    )
    return jsonify({"ok": True, "channels": [
        {"id": c["id"], "name": c["name"], "user_limit": c.get("user_limit")} for c in channels
    ]})


@app.route("/api/hang", methods=["POST"])
def api_hang():
    token, err = require_token()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    gid = str(data.get("guild_id") or "")
    cid = str(data.get("channel_id") or "")
    gname = str(data.get("guild_name") or "")
    cname = str(data.get("channel_name") or "")
    if not gid or not cid:
        return jsonify({"ok": False, "error": "Thiếu thông tin server/kênh"}), 400

    old = hangs.pop(token, None)
    if old:
        call(old["stop"].set)

    started = time.time()
    stop = submit(_make_event()).result()
    hangs[token] = {"stop": stop, "started_at": started}

    state = load_json(STATE_FILE)
    state[token] = {
        "guild_id": gid,
        "channel_id": cid,
        "guild_name": gname,
        "channel_name": cname,
        "started_at": started,
    }
    save_json(STATE_FILE, state)

    submit(hang_voice(token, gid, cid, stop))
    return jsonify({"ok": True, "started_at": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    token, err = require_token()
    if err:
        return err
    old = hangs.pop(token, None)
    if old:
        call(old["stop"].set)
    state = load_json(STATE_FILE)
    state.pop(token, None)
    save_json(STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    token, err = require_token()
    if err:
        return err
    state = load_json(STATE_FILE)
    info = state.get(token)
    hang = None
    if info:
        hang = {
            "guild_id": info["guild_id"],
            "channel_id": info["channel_id"],
            "guild_name": info.get("guild_name", ""),
            "channel_name": info.get("channel_name", ""),
            "started_at": info.get("started_at", time.time()),
        }
    sessions = load_json(SESSIONS_FILE)
    s = sessions.get(token, {})
    return jsonify({"ok": True, "hang": hang, "user": {
        "id": s.get("id", ""),
        "username": s.get("username", ""),
        "avatar": s.get("avatar"),
    }})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token, err = require_token()
    if err:
        return err
    old = hangs.pop(token, None)
    if old:
        call(old["stop"].set)
    state = load_json(STATE_FILE)
    state.pop(token, None)
    save_json(STATE_FILE, state)
    sessions = load_json(SESSIONS_FILE)
    sessions.pop(token, None)
    save_json(SESSIONS_FILE, sessions)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "time": int(time.time())})


@app.route("/style.css")
def css():
    return send_from_directory(BASE, "style.css")


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", "5000"))
    if "--dev" in sys.argv:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        from waitress import serve

        print(f"[WUMMI-HANG] Server chay tai: http://0.0.0.0:{port}")
        serve(app, host="0.0.0.0", port=port, threads=12)
