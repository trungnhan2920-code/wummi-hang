import asyncio
import base64
import json
import os
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime

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


# ================= AUTO QUEST DISCORD =================
QUEST_STATE_FILE = os.path.join(BASE, "quest_state.json")
QUEST_TASKS = ("WATCH_VIDEO", "PLAY_ON_DESKTOP", "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE", "STREAM_ON_DESKTOP")
DISCORD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9236 Chrome/138.0.7204.251 "
    "Electron/37.6.0 Safari/537.36"
)
quest_workers = {}


def quest_headers(token):
    props = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9236",
        "os_version": "10.0.19045",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "has_client_mods": False,
        "client_launch_id": str(uuid.uuid4()),
        "browser_user_agent": DISCORD_UA,
        "browser_version": "37.6.0",
        "os_sdk_version": "19045",
        "client_build_number": 539951,
        "native_build_number": 81687,
        "client_event_source": None,
        "launch_signature": str(uuid.uuid4()),
        "client_heartbeat_session_id": str(uuid.uuid4()),
        "client_app_state": "focused",
    }
    return {
        "Authorization": token,
        "User-Agent": DISCORD_UA,
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "x-super-properties": base64.b64encode(json.dumps(props).encode()).decode(),
        "x-discord-locale": "en-US",
        "x-discord-timezone": "Asia/Saigon",
        "x-debug-options": "bugReporterEnabled",
    }


def iso_ms(value):
    try:
        s = (value or "").replace("Z", "+00:00")
        if not s:
            return 0
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        try:
            return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
        except Exception:
            return 0


class QuestWorker:
    def __init__(self, token, auto_accept=True):
        self.token = token
        self.auto_accept = bool(auto_accept)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.logs = deque(maxlen=500)
        self.quests = []
        self.tasks = {}
        self.started_at = time.time()
        self.thread = None

    def log(self, msg):
        with self.lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def get_logs(self):
        with self.lock:
            return list(self.logs)

    def get_quests(self):
        with self.lock:
            return list(self.quests)

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    # ---------- helpers ----------
    @staticmethod
    def _task_config(quest):
        cfg = quest.get("config") or {}
        return (
            cfg.get("task_config_v2")
            or cfg.get("task_config")
            or cfg.get("taskConfigV2")
            or cfg.get("taskConfig")
            or {}
        )

    @staticmethod
    def _quest_name(quest):
        m = (quest.get("config") or {}).get("messages") or {}
        return m.get("quest_name") or m.get("questName") or quest.get("id", "?")

    @staticmethod
    def _user_status(quest):
        return quest.get("user_status") or {}

    def _enrolled(self, quest):
        return bool(self._user_status(quest).get("enrolled_at"))

    def _completed(self, quest):
        return bool(self._user_status(quest).get("completed_at"))

    def _completable(self, quest):
        cfg = quest.get("config") or {}
        try:
            if not cfg.get("expires_at"):
                return False
            if datetime.fromisoformat(str(cfg["expires_at"]).replace("Z", "+00:00")).timestamp() <= time.time():
                return False
        except Exception:
            pass
        tasks = self._task_config(quest).get("tasks") or {}
        return any(t in tasks for t in QUEST_TASKS)

    def _progress(self, body, task_name, quest):
        cfg = quest.get("config") or {}
        if cfg.get("config_version") == 1 and task_name in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
            try:
                return int(float(body.get("stream_progress_seconds") or body.get("streamProgressSeconds") or 0))
            except Exception:
                return 0
        try:
            return int((body.get("progress") or {}).get(task_name, {}).get("value") or 0)
        except Exception:
            return 0

    # ---------- HTTP ----------
    def _post(self, path, body):
        return requests.post(
            "https://discord.com/api/v9" + path,
            headers=quest_headers(self.token),
            json=body,
            timeout=30,
        )

    def _get(self, path):
        return requests.get(
            "https://discord.com/api/v9" + path,
            headers=quest_headers(self.token),
            timeout=30,
        )

    # ---------- main loop ----------
    def _run(self):
        self.log(f"[Auto Quest] Bắt đầu (auto nhận: {'BẬT' if self.auto_accept else 'TẮT'})")
        first = True
        while not self.stop_event.is_set():
            try:
                self._tick(first)
            except Exception as e:
                self.log(f"[Auto Quest] Lỗi vòng lặp: {e}")
            first = False
            self.stop_event.wait(60)
        with self.lock:
            self.tasks.clear()
        self.log("[Auto Quest] Đã dừng")

    def _tick(self, first):
        r = self._get("/quests/@me")
        if r.status_code == 401:
            self.log("[Auto Quest] Token hết hạn, dừng worker")
            self.stop_event.set()
            return
        if r.status_code != 200:
            self.log(f"[Auto Quest] Không tải được quest (HTTP {r.status_code})")
            return
        quests = (r.json() or {}).get("quests") or []
        with self.lock:
            self.quests = quests

        if self.auto_accept:
            pending = [
                q for q in quests
                if not self._enrolled(q) and not self._completed(q) and self._completable(q)
            ]
            if pending:
                self.log(f"Tự nhận {len(pending)} quest...")
                for q in pending:
                    if self.stop_event.is_set():
                        return
                    self._enroll(q)
                    self.stop_event.wait(3)

        for q in quests:
            if self.stop_event.is_set():
                return
            if self._enrolled(q) and not self._completed(q):
                qid = q.get("id")
                with self.lock:
                    if qid not in self.tasks:
                        t = threading.Thread(target=self._handle_quest, args=(q,), daemon=True)
                        self.tasks[qid] = t
                        t.start()

    def _enroll(self, quest):
        name = self._quest_name(quest)
        qid = quest.get("id", "")
        for attempt in (1, 2, 3):
            if self.stop_event.is_set():
                return False
            try:
                r = self._post(f"/quests/{qid}/enroll", {
                    "location": 11,
                    "is_targeted": False,
                    "metadata_raw": None,
                    "metadata_sealed": None,
                    "traffic_metadata_raw": quest.get("traffic_metadata_raw"),
                    "traffic_metadata_sealed": quest.get("traffic_metadata_sealed"),
                })
                if r.status_code == 429:
                    try:
                        wait = float(r.json().get("retry_after", 5)) + 1
                    except Exception:
                        wait = 6
                    self.log(f"Rate limited khi nhận \"{name}\" – chờ {int(wait)}s...")
                    if attempt < 3:
                        self.stop_event.wait(wait)
                    continue
                if r.status_code < 400:
                    self.log(f"Đã nhận quest: {name}")
                    return True
                self.log(f"Không nhận được \"{name}\" (HTTP {r.status_code}): {r.text[:160]}")
                return False
            except Exception as e:
                self.log(f"Lỗi khi nhận \"{name}\": {e}")
                return False
        self.log(f"Bỏ nhận \"{name}\" sau 3 lần rate limited")
        return False

    def _handle_quest(self, quest):
        qid = quest.get("id", "")
        try:
            tasks = self._task_config(quest).get("tasks") or {}
            task_name = next((t for t in QUEST_TASKS if t in tasks), None)
            if not task_name:
                self.log(f"Bỏ qua quest không hỗ trợ: {self._quest_name(quest)}")
                return
            task = tasks[task_name]
            if task_name in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                self._run_video(quest, task_name, task)
            elif task_name == "PLAY_ON_DESKTOP":
                self._run_play(quest, task_name, task)
            elif task_name == "PLAY_ACTIVITY":
                self._run_activity(quest, task_name, task)
            else:
                self.log(
                    f"Bỏ qua \"{self._quest_name(quest)}\": STREAM_ON_DESKTOP "
                    "chỉ chạy được trên app Discord desktop (stream thật + ít nhất 1 người xem)"
                )
        except Exception as e:
            self.log(f"Lỗi xử lý quest \"{self._quest_name(quest)}\": {e}")
        finally:
            with self.lock:
                self.tasks.pop(qid, None)

    def _run_video(self, quest, task_name, task):
        name = self._quest_name(quest)
        qid = quest["id"]
        needed = int(task.get("target") or 0)
        us = self._user_status(quest)
        done = self._progress(us, task_name, quest)
        enrolled_ms = iso_ms(us.get("enrolled_at"))
        try:
            self.log(f"[Video] {name}: {min(done, needed)}/{needed}s")
            while not self.stop_event.is_set():
                max_allowed = int((time.time() * 1000 - enrolled_ms) / 1000) + 10
                ts = done + 7
                if max_allowed - done >= 7:
                    r = self._post(f"/quests/{qid}/video-progress", {
                        "timestamp": min(needed, ts + random.random())
                    })
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    if r.status_code == 429:
                        self.stop_event.wait(5)
                        continue
                    if r.status_code >= 400:
                        self.log(f"[{name}] Lỗi video-progress (HTTP {r.status_code}): {str(body.get('message', r.text[:150]))}")
                        return
                    done = min(needed, ts)
                    if body.get("completed_at"):
                        self.log(f"Hoàn thành: {name}")
                        return
                    self.log(f"[{name}] Tiến độ: {done}/{needed}s")
                if ts >= needed:
                    break
                self.stop_event.wait(1)
            if not self.stop_event.is_set():
                self._post(f"/quests/{qid}/video-progress", {"timestamp": needed})
                self.log(f"Hoàn thành: {name}")
        except Exception as e:
            self.log(f"Lỗi video \"{name}\": {e}")

    def _run_play(self, quest, task_name, task):
        name = self._quest_name(quest)
        qid = quest["id"]
        needed = int(task.get("target") or 0)
        app_id = (quest.get("config") or {}).get("application", {}).get("id")
        self.log(f"[Game] {name}: giả lập chơi game trên desktop...")
        while not self.stop_event.is_set():
            try:
                r = self._post(f"/quests/{qid}/heartbeat", {
                    "application_id": app_id, "terminal": False
                })
                try:
                    body = r.json()
                except Exception:
                    body = {}
                if r.status_code == 429:
                    self.stop_event.wait(30)
                    continue
                if r.status_code >= 400:
                    self.log(f"[{name}] Lỗi heartbeat (HTTP {r.status_code}): {str(body.get('message', r.text[:150]))}")
                    return
                prog = self._progress(body, task_name, quest)
                self.log(f"[{name}] Tiến độ: {prog}/{needed}s")
                if prog >= needed:
                    self._post(f"/quests/{qid}/heartbeat", {
                        "application_id": app_id, "terminal": True
                    })
                    self.log(f"Hoàn thành: {name}")
                    return
            except Exception as e:
                self.log(f"Lỗi heartbeat \"{name}\": {e}")
                return
            self.stop_event.wait(20)

    def _run_activity(self, quest, task_name, task):
        name = self._quest_name(quest)
        qid = quest["id"]
        needed = int(task.get("target") or 0)
        stream_key = f"call:{qid}:1"
        self.log(f"[Activity] {name}: gửi heartbeat...")
        while not self.stop_event.is_set():
            try:
                r = self._post(f"/quests/{qid}/heartbeat", {
                    "stream_key": stream_key, "terminal": False
                })
                try:
                    body = r.json()
                except Exception:
                    body = {}
                if r.status_code == 429:
                    self.stop_event.wait(30)
                    continue
                if r.status_code >= 400:
                    self.log(f"[{name}] Lỗi heartbeat (HTTP {r.status_code}): {str(body.get('message', r.text[:150]))}")
                    return
                prog = self._progress(body, task_name, quest)
                self.log(f"[{name}] Tiến độ: {prog}/{needed}s")
                if prog >= needed:
                    self._post(f"/quests/{qid}/heartbeat", {
                        "stream_key": stream_key, "terminal": True
                    })
                    self.log(f"Hoàn thành: {name}")
                    return
            except Exception as e:
                self.log(f"Lỗi heartbeat \"{name}\": {e}")
                return
            self.stop_event.wait(20)


def summarize_quest(q):
    cfg = q.get("config") or {}
    us = q.get("user_status") or {}
    tasks = QuestWorker._task_config(q).get("tasks") or {}
    task_name = next((t for t in QUEST_TASKS if t in tasks), None)
    target, value = 0, 0
    if task_name:
        target = int(tasks[task_name].get("target") or 0)
        try:
            value = int((us.get("progress") or {}).get(task_name, {}).get("value") or 0)
        except Exception:
            value = 0
        if cfg.get("config_version") == 1 and task_name in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
            try:
                value = int(float(us.get("stream_progress_seconds") or us.get("streamProgressSeconds") or 0))
            except Exception:
                pass
    return {
        "id": q.get("id"),
        "name": QuestWorker._quest_name(q),
        "app": (cfg.get("application") or {}).get("name", ""),
        "task": task_name,
        "target": target,
        "value": value,
        "enrolled": bool(us.get("enrolled_at")),
        "completed": bool(us.get("completed_at")),
        "expires_at": cfg.get("expires_at"),
    }


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
    sessions = load_json(SESSIONS_FILE)
    qstate = load_json(QUEST_STATE_FILE)
    for token, info in qstate.items():
        if token not in sessions:
            continue
        w = QuestWorker(token, info.get("auto_accept", True))
        quest_workers[token] = w
        w.start()


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


@app.route("/api/quests")
def api_quests():
    token, err = require_token()
    if err:
        return err
    w = quest_workers.get(token)
    quests = w.get_quests() if w else None
    if quests is None:
        try:
            r = requests.get(
                "https://discord.com/api/v9/quests/@me",
                headers=quest_headers(token),
                timeout=20,
            )
            if r.status_code == 200:
                quests = (r.json() or {}).get("quests") or []
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "running": bool(w and w.is_alive()),
        "quests": [summarize_quest(q) for q in (quests or [])],
    })


@app.route("/api/quests/start", methods=["POST"])
def api_quests_start():
    token, err = require_token()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    auto_accept = bool(data.get("auto_accept", True))
    w = quest_workers.get(token)
    if not w:
        w = QuestWorker(token, auto_accept)
        quest_workers[token] = w
    else:
        w.auto_accept = auto_accept
    w.start()
    state = load_json(QUEST_STATE_FILE)
    state[token] = {"auto_accept": auto_accept, "started_at": w.started_at}
    save_json(QUEST_STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/quests/stop", methods=["POST"])
def api_quests_stop():
    token, err = require_token()
    if err:
        return err
    w = quest_workers.get(token)
    if w:
        w.stop()
    state = load_json(QUEST_STATE_FILE)
    state.pop(token, None)
    save_json(QUEST_STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/quests/status")
def api_quests_status():
    token, err = require_token()
    if err:
        return err
    w = quest_workers.get(token)
    return jsonify({
        "ok": True,
        "running": bool(w and w.is_alive()),
        "auto_accept": bool(w and w.auto_accept),
        "started_at": w.started_at if w else None,
        "logs": w.get_logs() if w else [],
    })


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
    w = quest_workers.pop(token, None)
    if w:
        w.stop()
    qstate = load_json(QUEST_STATE_FILE)
    qstate.pop(token, None)
    save_json(QUEST_STATE_FILE, qstate)
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
