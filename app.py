import asyncio
import base64
import json
import os
import random
import re
import socket
import struct
import threading
import time
from collections import deque
from datetime import datetime

import requests
import websockets
from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE = os.path.join(BASE, "sessions.json")
STATE_FILE = os.path.join(BASE, "state.json")
ASSETS = os.path.join(BASE, "assets")
GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"

app = Flask(__name__)

hangs = {}
xa_senders = {}  # token -> {key -> {"stop": Event, "started_at": float}}
_xa_frames_cache = None
_loop = None
_loop_thread = None
_restored = False

XA_FILE = os.path.join(BASE, "xa.mp3")


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
QUEST_TASKS = ("WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE")
VIDEO_SPEED = 7          # giây nhảy mỗi lần báo tiến độ video
VIDEO_INTERVAL = 1       # giây giữa các lần báo
VIDEO_MAX_FUTURE = 10    # giới hạn "tương lai" so với thời gian thực
HEARTBEAT_INTERVAL = 20  # giây giữa các heartbeat
DISCORD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 "
    "Electron/32.2.7 Safari/537.36"
)
DEFAULT_BUILD_NUMBER = 504649
_build_number = None
quest_workers = {}


def fetch_latest_build_number():
    global _build_number
    if _build_number is not None:
        return _build_number
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    try:
        r = requests.get("https://discord.com/app", headers={"User-Agent": ua}, timeout=15)
        if r.status_code == 200:
            assets = re.findall(r'/assets/([a-f0-9]+)\.js', r.text)
            if not assets:
                assets = [
                    a.split("/")[-1].replace(".js", "") or a.split("/")[-1]
                    for a in re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
                ]
            for asset in assets[-5:]:
                try:
                    ar = requests.get(
                        f"https://discord.com/assets/{asset}.js",
                        headers={"User-Agent": ua},
                        timeout=15,
                    )
                    m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', ar.text)
                    if m:
                        _build_number = int(m.group(1))
                        return _build_number
                except Exception:
                    continue
    except Exception:
        pass
    _build_number = DEFAULT_BUILD_NUMBER
    return _build_number


def make_quest_session(token):
    build = fetch_latest_build_number()
    props = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": "1.0.9175",
        "os_version": "10.0.26100",
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": DISCORD_UA,
        "browser_version": "32.2.7",
        "client_build_number": build,
        "native_build_number": 59498,
        "client_event_source": None,
    }
    s = requests.Session()
    s.headers.update({
        "Authorization": token,
        "User-Agent": DISCORD_UA,
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "X-Super-Properties": base64.b64encode(json.dumps(props).encode()).decode(),
        "X-Discord-Locale": "en-US",
        "X-Discord-Timezone": "Asia/Ho_Chi_Minh",
    })
    return s


class QuestWorker:
    def __init__(self, token, auto_accept=True):
        self.token = token
        self.auto_accept = bool(auto_accept)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.logs = deque(maxlen=500)
        self.quests = []
        self.tasks = {}
        self.progress = {}
        self.started_at = time.time()
        self.thread = None
        self.session = None

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
        us = quest.get("user_status")
        if not isinstance(us, dict):
            us = quest.get("userStatus")
        return us if isinstance(us, dict) else {}

    def _enrolled(self, quest):
        return bool(self._user_status(quest).get("enrolled_at") or self._user_status(quest).get("enrolledAt"))

    def _completed(self, quest):
        return bool(self._user_status(quest).get("completed_at") or self._user_status(quest).get("completedAt"))

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
    def _ensure_session(self):
        if self.session is None:
            self.session = make_quest_session(self.token)
        return self.session

    def _post(self, path, body):
        return self._ensure_session().post(
            "https://discord.com/api/v9" + path,
            json=body,
            timeout=30,
        )

    def _get(self, path):
        return self._ensure_session().get(
            "https://discord.com/api/v9" + path,
            timeout=30,
        )

    def _set_progress(self, qid, value, needed):
        with self.lock:
            self.progress[qid] = {"value": value, "needed": needed}

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
        for attempt in (1, 2):
            r = self._get("/quests/@me")
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 10)) + 1
                except Exception:
                    wait = 11
                self.log(f"[Auto Quest] Rate limited – chờ {int(wait)}s...")
                self.stop_event.wait(wait)
                continue
            break
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

        # Dọn progress của quest không còn xuất hiện
        valid_ids = {q.get("id") for q in quests}
        with self.lock:
            for qid in [x for x in self.progress if x not in valid_ids]:
                self.progress.pop(qid, None)

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

        new_threads = 0
        for q in quests:
            if self.stop_event.is_set():
                return
            if self._enrolled(q) and not self._completed(q) and self._completable(q):
                qid = q.get("id")
                with self.lock:
                    if qid not in self.tasks:
                        t = threading.Thread(target=self._handle_quest, args=(q,), daemon=True)
                        self.tasks[qid] = t
                        t.start()
                        new_threads += 1
        if new_threads:
            self.log(f"Chạy song song {new_threads} quest mới (tất cả cùng lúc)")

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
            elif task_name in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
                self._run_heartbeat(quest, task_name, task)
            elif task_name == "PLAY_ACTIVITY":
                self._run_activity(quest, task_name, task)
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
        enrolled_at_str = us.get("enrolled_at") or us.get("enrolledAt")
        if enrolled_at_str:
            try:
                enrolled_ts = datetime.fromisoformat(str(enrolled_at_str).replace("Z", "+00:00")).timestamp()
            except Exception:
                enrolled_ts = time.time()
        else:
            enrolled_ts = time.time()

        self.log(f"[Video] {name}: {min(done, needed):.0f}/{needed}s (chạy song song)")

        while done < needed and not self.stop_event.is_set():
            max_allowed = (time.time() - enrolled_ts) + VIDEO_MAX_FUTURE
            diff = max_allowed - done
            timestamp = done + VIDEO_SPEED

            if diff >= VIDEO_SPEED:
                try:
                    r = self._post(f"/quests/{qid}/video-progress", {
                        "timestamp": min(needed, timestamp + random.random())
                    })
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    if r.status_code == 200:
                        if body.get("completed_at"):
                            self.log(f"Hoàn thành: {name}")
                            return
                        done = min(needed, timestamp)
                        self._set_progress(qid, done, needed)
                        self.log(f"[{name}] Tiến độ: {done:.0f}/{needed}s")
                    elif r.status_code == 429:
                        try:
                            wait = float(body.get("retry_after", 5)) + 1
                        except Exception:
                            wait = 6
                        self.log(f"[{name}] Rate limited – chờ {int(wait)}s...")
                        self.stop_event.wait(wait)
                        continue
                    else:
                        self.log(f"[{name}] Lỗi video-progress (HTTP {r.status_code}): {r.text[:150]}")
                except Exception as e:
                    self.log(f"Lỗi video \"{name}\": {e}")

            if timestamp >= needed:
                break
            self.stop_event.wait(VIDEO_INTERVAL)

        if not self.stop_event.is_set():
            try:
                self._post(f"/quests/{qid}/video-progress", {"timestamp": needed})
            except Exception:
                pass
            self.log(f"Hoàn thành: {name}")

    def _run_heartbeat(self, quest, task_name, task):
        name = self._quest_name(quest)
        qid = quest["id"]
        needed = int(task.get("target") or 0)
        pid = random.randint(1000, 30000)
        stream_key = f"call:0:{pid}"
        us = self._user_status(quest)
        done = self._progress(us, task_name, quest)
        remaining = max(0, needed - done)
        self.log(f"[{task_name}] {name}: giả lập (còn ~{remaining // 60} phút, chạy song song)")
        fails = 0
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
                    try:
                        wait = float(body.get("retry_after", 10)) + 1
                    except Exception:
                        wait = 11
                    self.log(f"[{name}] Rate limited – chờ {int(wait)}s...")
                    self.stop_event.wait(wait)
                    continue
                if r.status_code >= 400:
                    fails += 1
                    self.log(f"[{name}] Lỗi heartbeat (HTTP {r.status_code}): {str(body.get('message', r.text[:150]))}")
                    if fails >= 5:
                        self.log(f"[{name}] Bỏ sau 5 lần lỗi liên tiếp")
                        return
                    self.stop_event.wait(20)
                    continue
                fails = 0
                prog = self._progress(body, task_name, quest)
                self._set_progress(qid, prog, needed)
                self.log(f"[{name}] Tiến độ: {prog}/{needed}s")
                if body.get("completed_at") or prog >= needed:
                    self._post(f"/quests/{qid}/heartbeat", {
                        "stream_key": stream_key, "terminal": True
                    })
                    self.log(f"Hoàn thành: {name}")
                    return
            except Exception as e:
                fails += 1
                self.log(f"Lỗi heartbeat \"{name}\": {e}")
                if fails >= 5:
                    return
            self.stop_event.wait(20)

    def _run_activity(self, quest, task_name, task):
        name = self._quest_name(quest)
        qid = quest["id"]
        needed = int(task.get("target") or 0)
        stream_key = "call:0:1"
        us = self._user_status(quest)
        done = self._progress(us, task_name, quest)
        remaining = max(0, needed - done)
        self.log(f"[Activity] {name}: gửi heartbeat (còn ~{remaining // 60} phút, chạy song song)")
        fails = 0
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
                    try:
                        wait = float(body.get("retry_after", 10)) + 1
                    except Exception:
                        wait = 11
                    self.log(f"[{name}] Rate limited – chờ {int(wait)}s...")
                    self.stop_event.wait(wait)
                    continue
                if r.status_code >= 400:
                    fails += 1
                    self.log(f"[{name}] Lỗi heartbeat (HTTP {r.status_code}): {str(body.get('message', r.text[:150]))}")
                    if fails >= 5:
                        self.log(f"[{name}] Bỏ sau 5 lần lỗi liên tiếp")
                        return
                    self.stop_event.wait(20)
                    continue
                fails = 0
                prog = self._progress(body, task_name, quest)
                self._set_progress(qid, prog, needed)
                self.log(f"[{name}] Tiến độ: {prog}/{needed}s")
                if body.get("completed_at") or prog >= needed:
                    self._post(f"/quests/{qid}/heartbeat", {
                        "stream_key": stream_key, "terminal": True
                    })
                    self.log(f"Hoàn thành: {name}")
                    return
            except Exception as e:
                fails += 1
                self.log(f"Lỗi heartbeat \"{name}\": {e}")
                if fails >= 5:
                    return
            self.stop_event.wait(20)


def summarize_quest(q):
    cfg = q.get("config") or {}
    us = QuestWorker._user_status(q)
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
        "enrolled": bool(us.get("enrolled_at") or us.get("enrolledAt")),
        "completed": bool(us.get("completed_at") or us.get("completedAt")),
        "expires_at": cfg.get("expires_at") or cfg.get("expiresAt"),
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


# ================= XẢ MIC (phát âm thanh vào kênh voice, lặp vô hạn) =================

def prepare_xa_frames():
    global _xa_frames_cache
    if _xa_frames_cache is not None:
        return _xa_frames_cache
    if not os.path.exists(XA_FILE):
        return None
    try:
        os.environ["PATH"] = BASE + os.pathsep + os.environ.get("PATH", "")
        from pyogg import OpusFile
        import opuslib
        import numpy as np
        of = OpusFile(XA_FILE)
        n = of.buffer_length // 2
        arr = np.ctypeslib.as_array(of.buffer, shape=(n,))
        if of.channels >= 2:
            st = np.ascontiguousarray(arr.reshape(-1, of.channels)[:, :2])
        else:
            st = np.ascontiguousarray(np.repeat(arr.reshape(-1, 1), 2, axis=1))
        peak = max(1, int(np.abs(st).max()))
        amp = np.clip(st.astype(np.float64) * (0.95 / peak * 6.0), -32767, 32767).astype(np.int16)
        pcm = np.ascontiguousarray(amp[::2]).tobytes()
        frame_bytes = 960 * 2 * 2
        nf = len(pcm) // frame_bytes
        if nf == 0:
            return None
        enc = opuslib.Encoder(48000, 2, opuslib.APPLICATION_AUDIO)
        frames = [enc.encode(pcm[i * frame_bytes:(i + 1) * frame_bytes], 960) for i in range(nf)]
        _xa_frames_cache = frames
        return frames
    except Exception:
        return None


async def xa_voice(token, guild_id, channel_id, stop_event):
    frames = prepare_xa_frames()
    if not frames:
        return
    user_id = ""
    while not stop_event.is_set():
        try:
            if not user_id:
                try:
                    user_id = dapi("GET", "/users/@me", token).json()["id"]
                except Exception:
                    pass
            await _xa_pipeline(token, guild_id, channel_id, stop_event, frames, user_id)
        except asyncio.CancelledError:
            return
        except Exception:
            if stop_event.is_set():
                return
            await asyncio.sleep(5)


async def _xa_pipeline(token, guild_id, channel_id, stop_event, frames, user_id):
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
            await ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": False,
                    "self_deaf": False,
                    "self_video": False,
                },
            }))
            session_id = None
            endpoint = None
            vtoken = None
            while session_id is None or endpoint is None:
                if stop_event.is_set():
                    return
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if ev["op"] == 0:
                    if ev["t"] == "VOICE_STATE_UPDATE":
                        d = ev["d"]
                        if d.get("guild_id") == guild_id and d.get("channel_id") == channel_id:
                            session_id = d.get("session_id")
                    elif ev["t"] == "VOICE_SERVER_UPDATE":
                        d = ev["d"]
                        if d.get("guild_id") == guild_id:
                            endpoint = (d.get("endpoint") or "").strip()
                            vtoken = d.get("token")
            await _xa_voice_session(frames, stop_event, user_id, guild_id, channel_id, session_id, endpoint, vtoken)
        finally:
            hb_task.cancel()


async def _xa_voice_session(frames, stop_event, user_id, guild_id, channel_id, session_id, endpoint, vtoken):
    from nacl.secret import SecretBox
    uri = "wss://" + endpoint + "/?v=4"
    host = endpoint.split(":")[0]
    loop = asyncio.get_event_loop()
    async with websockets.connect(uri, max_size=None, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "op": 0,
            "d": {
                "server_id": guild_id,
                "user_id": user_id,
                "session_id": session_id,
                "token": vtoken,
            },
        }))
        ssrc = port = mode = None
        while ssrc is None:
            if stop_event.is_set():
                return
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if msg.get("op") == 2:
                d = msg["d"]
                ssrc = d.get("ssrc")
                port = int(d.get("port") or 0)
                modes = d.get("modes") or []
                mode = "xsalsa20_poly1305" if "xsalsa20_poly1305" in modes else (modes[0] if modes else "plain")
            elif msg.get("op") == 8:
                raise ConnectionError("voice websocket closed")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.connect((host, port))
        except OSError:
            pass
        await loop.sock_sendto(sock, struct.pack(">I", ssrc) + b"\x00" * 66, (host, port))
        data = await loop.sock_recv(sock, 74)
        dip = data[4:68].split(b"\x00")[0].decode(errors="ignore") or host
        dport = struct.unpack(">H", data[68:70])[0] or port
        await ws.send(json.dumps({
            "op": 1,
            "d": {"protocol": "udp", "data": {"address": dip, "port": dport, "mode": mode}},
        }))
        secret = None
        while secret is None:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if msg.get("op") == 4:
                secret = bytes(msg["d"]["secret_key"])
        await ws.send(json.dumps({"op": 3, "d": None}))
        use_crypto = mode.startswith("xsalsa")
        box = SecretBox(secret) if use_crypto else None
        seq = random.randrange(0, 65536)
        ts = random.randrange(0, 2 ** 32)
        nframes = len(frames)
        i = 0
        await ws.send(json.dumps({"op": 5, "d": {"speaking": 1, "delay": 0, "ssrc": ssrc}}))
        try:
            while not stop_event.is_set():
                hdr = struct.pack(">BBHII", 0x80, 0x78, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
                if use_crypto:
                    nonce = hdr + b"\x00" * 12
                    pkt = hdr + box.encrypt(frames[i % nframes], nonce)[24:]
                else:
                    pkt = hdr + frames[i % nframes]
                seq = (seq + 1) & 0xFFFF
                ts = (ts + 960) & 0xFFFFFFFF
                i += 1
                try:
                    await loop.sock_sendto(sock, pkt, (host, port))
                except OSError:
                    break
                await asyncio.sleep(0.02)
        finally:
            try:
                await ws.send(json.dumps({"op": 5, "d": {"speaking": 0, "delay": 0, "ssrc": ssrc}}))
            except Exception:
                pass
            sock.close()


@app.before_request
def restore_hangs():
    global _restored
    if _restored:
        return
    _restored = True
    state = load_json(STATE_FILE)
    for token, info in state.items():
        if not isinstance(info, dict):
            continue
        hs = info.get("hangs")
        if not isinstance(hs, dict):
            if info.get("guild_id"):
                hs = {f"{info['guild_id']}:{info['channel_id']}": info}
            else:
                hs = {}
        for key, hin in hs.items():
            if not isinstance(hin, dict) or not hin.get("guild_id"):
                continue
            stop = submit(_make_event()).result()
            hangs.setdefault(token, {})[key] = {"stop": stop, "started_at": hin.get("started_at", time.time())}
            submit(hang_voice(token, hin["guild_id"], hin["channel_id"], stop))
            if hin.get("xamic"):
                xstop = submit(_make_event()).result()
                xa_senders.setdefault(token, {})[key] = {"stop": xstop, "started_at": time.time()}
                submit(xa_voice(token, hin["guild_id"], hin["channel_id"], xstop))
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

    key = f"{gid}:{cid}"
    existing = hangs.get(token, {}).get(key)
    if existing:
        return jsonify({"ok": True, "started_at": existing["started_at"], "key": key})

    started = time.time()
    stop = submit(_make_event()).result()
    hangs.setdefault(token, {})[key] = {"stop": stop, "started_at": started}

    state = load_json(STATE_FILE)
    st = state.setdefault(token, {})
    st.setdefault("hangs", {})[key] = {
        "guild_id": gid,
        "channel_id": cid,
        "guild_name": gname,
        "channel_name": cname,
        "started_at": started,
    }
    save_json(STATE_FILE, state)

    submit(hang_voice(token, gid, cid, stop))
    return jsonify({"ok": True, "started_at": started, "key": key})


@app.route("/api/xamic", methods=["POST"])
def api_xamic():
    token, err = require_token()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    gid = str(data.get("guild_id") or "")
    cid = str(data.get("channel_id") or "")
    action = str(data.get("action") or "start")
    if not gid or not cid:
        return jsonify({"ok": False, "error": "Thiếu thông tin server/kênh"}), 400
    key = f"{gid}:{cid}"
    senders = xa_senders.setdefault(token, {})
    if action == "stop":
        s = senders.pop(key, None)
        if s:
            call(s["stop"].set)
        state = load_json(STATE_FILE)
        st = state.get(token)
        if isinstance(st, dict) and isinstance(st.get("hangs"), dict):
            if key in st["hangs"]:
                st["hangs"][key].pop("xamic", None)
                save_json(STATE_FILE, state)
        return jsonify({"ok": True})
    if key in senders:
        return jsonify({"ok": True})
    stop = submit(_make_event()).result()
    senders[key] = {"stop": stop, "started_at": time.time()}
    state = load_json(STATE_FILE)
    st = state.setdefault(token, {}).setdefault("hangs", {})
    if key not in st:
        st[key] = {
            "guild_id": gid,
            "channel_id": cid,
            "guild_name": str(data.get("guild_name") or ""),
            "channel_name": str(data.get("channel_name") or ""),
            "started_at": time.time(),
        }
    st[key]["xamic"] = True
    save_json(STATE_FILE, state)
    submit(xa_voice(token, gid, cid, stop))
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    token, err = require_token()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    gid = str(data.get("guild_id") or "")
    cid = str(data.get("channel_id") or "")
    state = load_json(STATE_FILE)
    if gid and cid:
        key = f"{gid}:{cid}"
        h = hangs.get(token, {}).pop(key, None)
        if h:
            call(h["stop"].set)
        x = xa_senders.get(token, {}).pop(key, None)
        if x:
            call(x["stop"].set)
        st = state.get(token)
        if isinstance(st, dict) and isinstance(st.get("hangs"), dict):
            st["hangs"].pop(key, None)
            if not st["hangs"]:
                state.pop(token, None)
        save_json(STATE_FILE, state)
    else:
        for h in (hangs.pop(token, {}) or {}).values():
            call(h["stop"].set)
        for x in (xa_senders.pop(token, {}) or {}).values():
            call(x["stop"].set)
        state.pop(token, None)
        save_json(STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    token, err = require_token()
    if err:
        return err
    state = load_json(STATE_FILE)
    info = state.get(token) or {}
    hs = info.get("hangs")
    if not isinstance(hs, dict):
        if isinstance(info, dict) and info.get("guild_id"):
            hs = {f"{info['guild_id']}:{info['channel_id']}": info}
        else:
            hs = {}
    hang_list = []
    for key, hin in hs.items():
        if not isinstance(hin, dict) or not hin.get("guild_id"):
            continue
        hang_list.append({
            "key": key,
            "guild_id": hin.get("guild_id"),
            "channel_id": hin.get("channel_id"),
            "guild_name": hin.get("guild_name", ""),
            "channel_name": hin.get("channel_name", ""),
            "started_at": hin.get("started_at", time.time()),
            "xamic": bool(hin.get("xamic")),
        })
    sessions = load_json(SESSIONS_FILE)
    s = sessions.get(token, {})
    return jsonify({"ok": True, "hangs": hang_list, "user": {
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
            r = make_quest_session(token).get(
                "https://discord.com/api/v9/quests/@me",
                timeout=20,
            )
            if r.status_code == 200:
                quests = (r.json() or {}).get("quests") or []
        except Exception:
            pass
    summaries = [summarize_quest(q) for q in (quests or [])]
    if w:
        with w.lock:
            prog = {k: dict(v) for k, v in w.progress.items()}
        for s in summaries:
            p = prog.get(s["id"])
            if p and not s["completed"] and s["target"]:
                s["value"] = max(s["value"], min(s["target"], int(p.get("value") or 0)))
    return jsonify({
        "ok": True,
        "running": bool(w and w.is_alive()),
        "quests": summaries,
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
    for h in (hangs.pop(token, {}) or {}).values():
        call(h["stop"].set)
    for x in (xa_senders.pop(token, {}) or {}).values():
        call(x["stop"].set)
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


@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    return resp


@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.route("/assets/<path:name>")
def assets(name):
    if ".." in name or name.startswith(".") or "\\" in name:
        return jsonify({"ok": False, "error": "Not Found"}), 404
    return send_from_directory(ASSETS, name)


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "time": int(time.time())})


@app.route("/style.css")
def css():
    return send_from_directory(ASSETS, "style.min.css")


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", "5000"))
    if "--dev" in sys.argv:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        from waitress import serve

        print(f"[WUMMI-HANG] Server chay tai: http://0.0.0.0:{port}")
        serve(app, host="0.0.0.0", port=port, threads=12)
