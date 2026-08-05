/* ============================================================
   WUMMI HANGER — front logic (source)
   Build chạy: python build.py  ->  obfuscate + minify -> assets/
   ============================================================ */

(function () {
  "use strict";

  /* ---------------- chống mở mã / soi source ---------------- */
  (function antiTheft() {
    var block = function (e) {
      e.preventDefault();
      e.stopPropagation();
      return false;
    };
    document.addEventListener("contextmenu", block);
    document.addEventListener("dragstart", block);
    document.addEventListener("selectstart", function (e) {
      if (!/INPUT|TEXTAREA/.test((e.target && e.target.tagName) || "")) block(e);
    });
    document.addEventListener("keydown", function (e) {
      var k = (e.key || "").toLowerCase();
      var ctrl = e.ctrlKey || e.metaKey;
      if (e.key === "F12") return block(e);
      if (ctrl && ["u", "s", "i", "j", "c", "p", "e"].indexOf(k) > -1) return block(e);
    });
    var detected = function () {
      if (window.outerWidth - window.innerWidth > 150) return true;
      if (window.outerHeight - window.innerHeight > 150) return true;
      return false;
    };
    var dead = false;
    var kill = function () {
      if (dead) return;
      dead = true;
      document.body.innerHTML =
        '<div style="position:fixed;inset:0;background:#070810;color:#8b8fb3;font-family:system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;padding:24px;z-index:9999">' +
        '<div style="font-size:40px">🔒</div>' +
        '<div style="font-size:15px;font-weight:700;color:#e7e9ff">Phiên bị gián đoạn</div>' +
        '<div style="font-size:12.5px;max-width:340px;line-height:1.6">Tải lại trang và đóng cửa sổ kiểm tra để tiếp tục sử dụng WUMMI.</div>' +
        "</div>";
      clearInterval(det_int);
    };
    var det_int = setInterval(function () {
      if (detected()) kill();
    }, 900);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      if (detected()) kill();
    });
  })();

  /* ---------------- tiện ích ---------------- */
  var $ = function (s) {
    return document.querySelector(s);
  };
  var $$ = function (s) {
    return document.querySelectorAll(s);
  };
  var shown = function (el) {
    return el && el.classList.remove("hidden");
  };
  var hidden = function (el) {
    return el && el.classList.add("hidden");
  };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var delayFor = function (i) {
    return (i % 12) * 40;
  };

  var TOKEN = localStorage.getItem("wummi_token") || "";
  var USER = null;
  var GUILDS = [];
  var GUILD_FILTER = "";
  var ACTIVE_GUILD = null;
  var HANGS = {};
  var XAMIC = {};
  var TIMER_INT = null;
  var QUEST_POLL = null;

  var TASK_LABEL = {
    WATCH_VIDEO: "XEM VIDEO",
    WATCH_VIDEO_ON_MOBILE: "XEM VIDEO MOBILE",
    PLAY_ON_DESKTOP: "CHƠI GAME",
    PLAY_ACTIVITY: "ACTIVITY",
    STREAM_ON_DESKTOP: "STREAM",
  };

  function api(path, method, body) {
    return fetch(path, {
      method: method || "GET",
      headers: { "Content-Type": "application/json", "X-Token": TOKEN },
      body: body ? JSON.stringify(body) : null,
    }).then(function (r) {
      return r.json();
    });
  }

  function toast(msg, type) {
    var t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast show " + (type || "ok");
    clearTimeout(t._h);
    t._h = setTimeout(function () {
      t.classList.remove("show");
    }, 3200);
  }

  function iconUrl(g) {
    return g && g.icon ? "https://cdn.discordapp.com/icons/" + g.id + "/" + g.icon + ".png?size=128" : null;
  }

  function avatarUrl(u) {
    return u && u.avatar ? "https://cdn.discordapp.com/avatars/" + u.id + "/" + u.avatar + ".png?size=64" : null;
  }

  function showView(name) {
    $$(".view").forEach(function (v) {
      v.classList.remove("active");
    });
    var el = $("#view-" + name);
    if (el) el.classList.add("active");
  }

  function showMsg(text, isErr) {
    var m = $("#login-msg");
    if (!m) return;
    m.textContent = text;
    m.className = "msg " + (isErr ? "err" : "ok");
  }

  function fmtDuration(s) {
    var h = String(Math.floor(s / 3600)).padStart(2, "0");
    var m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    var sec = String(s % 60).padStart(2, "0");
    return h + ":" + m + ":" + sec;
  }

  /* ============================================================
     DASHBOARD (dựng bằng JS để không nằm trong HTML)
     ============================================================ */
  function dashShell() {
    return (
      '<header class="topbar glass">' +
      '<div class="brand">WUMMI<span class="grad">HANGER</span></div>' +
      '<div id="user-chip" class="user-chip"></div>' +
      '<button id="btn-logout" class="btn-ghost">Đăng xuất</button>' +
      "</header>" +
      '<main class="dash">' +

      '<aside class="panel glass guild-panel">' +
      '<div class="panel-head"><h3>SERVERS</h3><span id="guild-count" class="count">0</span></div>' +
      '<div class="search-box"><span class="search-ic">&#128269;</span><input id="guild-search" type="text" placeholder="Tìm server..." autocomplete="off" spellcheck="false"></div>' +
      '<div id="guild-list" class="list"></div>' +
      "</aside>" +

      '<section class="panel glass chan-panel">' +
      '<div class="panel-head"><h3 id="chan-title">CHỌN SERVER</h3></div>' +
      '<div id="channel-list" class="list"></div>' +
      "</section>" +

      '<aside class="panel glass hang-panel">' +
      '<div class="panel-head"><h3>VOICE HANG</h3><span id="hang-count" class="count">0</span></div>' +
      '<div class="hang-box">' +
      '<div id="hang-status" class="hang-status idle">Chưa treo kênh nào</div>' +
      '<div id="hang-list" class="hang-list"></div>' +
      '<button id="btn-stop-all" class="btn-danger hidden">Ngừng tất cả voice</button>' +
      "</div>" +
      '<div class="hang-note">Treo voice nhiều server cùng lúc &mdash; kết nối chạy trên máy chủ, đóng web vẫn giữ. Bấm kênh đang treo để ngừng kênh đó.</div>' +
      "</aside>" +

      '<section class="panel glass quest-panel">' +
      '<div class="panel-head">' +
      "<h3>AUTO QUEST <span id=\"quest-count\" class=\"count\">0</span></h3>" +
      '<div class="quest-actions">' +
      '<label class="switch"><input type="checkbox" id="quest-auto-accept" checked><span>Auto nhận quest</span></label>' +
      '<button id="btn-quest-toggle" class="btn-join">Bật Auto Quest</button>' +
      "</div>" +
      "</div>" +
      '<div id="quest-list" class="quest-list"></div>' +
      '<div class="quest-log-head"><h3>NHẬT KÝ</h3><span id="quest-running" class="qstate idle">ĐANG TẮT</span></div>' +
      '<div id="quest-log" class="quest-log">Chưa có nhật ký. Bật Auto Quest để bắt đầu.</div>' +
      '<div class="hang-note">Tự nhận quest và hoàn thành (xem video / chơi game / stream / activity). Tất cả quest chạy <b>song song cùng lúc</b>. Không nên chạy khi đang online trên thiết bị khác.</div>' +
      "</section>" +
      "</main>"
    );
  }

  /* ---------------- hang / timer ---------------- */
  function renderHangPanel() {
    var keys = Object.keys(HANGS);
    var status = $("#hang-status");
    var box = $("#hang-list");
    if (!$("#hang-count")) return;
    $("#hang-count").textContent = keys.length;
    if (!keys.length) {
      status.className = "hang-status idle";
      status.textContent = "Chưa treo kênh nào";
      box.innerHTML = "";
      hidden($("#btn-stop-all"));
      return;
    }
    var xaOn = keys.some(function (k) {
      return !!XAMIC[k];
    });
    status.className = "hang-status live";
    status.textContent = "ĐANG TREO " + keys.length + " KÊNH" + (xaOn ? " • XẢ MIC" : "");
    shown($("#btn-stop-all"));
    box.innerHTML = "";
    keys.forEach(function (key, i) {
      var h = HANGS[key];
      if (!h) return;
      var s = Math.max(0, Math.floor((Date.now() - h.started_at * 1000) / 1000));
      var isXa = !!XAMIC[key];
      var el = document.createElement("div");
      el.className = "hang-item";
      el.style.animationDelay = delayFor(i) + "ms";
      el.innerHTML =
        '<div class="hang-item-info">' +
        "<b>" + esc(h.guild_name) + " / " + esc(h.channel_name) + "</b>" +
        '<span class="hang-item-time">' + fmtDuration(s) + "</span>" +
        "</div>" +
        '<div class="hang-item-actions">' +
        '<button type="button" class="btn-xa sm' + (isXa ? " active" : "") + '">' +
        (isXa ? "Ngừng xả mic" : "Xả mic") +
        "</button>" +
        '<button type="button" class="btn-danger sm">Ngừng</button>' +
        "</div>";
      el.querySelector(".btn-xa").addEventListener("click", function () {
        toggleXamic(key);
      });
      el.querySelector(".btn-danger").addEventListener("click", function () {
        stopHang(key);
      });
      box.appendChild(el);
    });
  }

  function toggleXamic(key) {
    var h = HANGS[key];
    if (!h) return;
    var on = !!XAMIC[key];
    api("/api/xamic", "POST", {
      action: on ? "stop" : "start",
      guild_id: h.guild_id,
      channel_id: h.channel_id,
      guild_name: h.guild_name,
      channel_name: h.channel_name,
    }).then(function (r) {
      if (!r.ok) return toast(r.error || "Thao tác thất bại", "err");
      XAMIC[key] = !on;
      renderHangPanel();
      toast(on ? "Đã ngừng xả mic" : "Xả mic đang phát liên tục", on ? "ok" : "err");
    });
  }

  function startTimers() {
    clearInterval(TIMER_INT);
    renderHangPanel();
    TIMER_INT = setInterval(renderHangPanel, 1000);
  }

  function isHanging(gid, cid) {
    return !!HANGS[gid + ":" + cid];
  }

  /* ---------------- login ---------------- */
  function doLogin() {
    var token = $("#token-input").value.trim();
    if (!token) return toast("Vui lòng nhập token", "err");
    var btn = $("#btn-login");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Đang kết nối...';
    showMsg("", false);
    api("/api/login", "POST", { token: token }).then(function (r) {
      btn.disabled = false;
      btn.innerHTML = 'Kết nối <span class="btn-arrow">&rarr;</span>';
      if (!r.ok) {
        showMsg(r.error || "Đăng nhập thất bại", true);
        return;
      }
      TOKEN = token;
      try {
        localStorage.setItem("wummi_token", token);
      } catch (e) {}
      USER = r.user;
      loadDashboard();
    });
  }

  /* ---------------- dashboard ---------------- */
  function renderUserChip() {
    var chip = $("#user-chip");
    if (!chip || !USER) return;
    var av = avatarUrl(USER);
    if (av) {
      chip.innerHTML =
        '<img class="avatar" src="' + av + '" alt="">' +
        '<div class="uinfo"><b>' + esc(USER.username) + "</b><span>" + esc(USER.id) + "</span></div>";
    } else {
      chip.innerHTML =
        '<div class="avatar ph">' + esc((USER.username || "?").charAt(0).toUpperCase()) + "</div>" +
        '<div class="uinfo"><b>' + esc(USER.username) + "</b><span>" + esc(USER.id) + "</span></div>";
    }
  }

  function filteredGuilds() {
    var f = GUILD_FILTER.trim().toLowerCase();
    if (!f) return GUILDS;
    return GUILDS.filter(function (g) {
      return (g.name || "").toLowerCase().indexOf(f) > -1 || (g.id || "").indexOf(f) > -1;
    });
  }

  function renderGuilds() {
    var list = $("#guild-list");
    if (!list) return;
    var items = filteredGuilds();
    $("#guild-count").textContent = GUILDS.length;
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = GUILDS.length
        ? '<div class="empty">Không tìm thấy server</div>'
        : '<div class="empty">Không có server nào</div>';
      return;
    }
    items.forEach(function (g, i) {
      var item = document.createElement("div");
      item.className = "guild-item" + (ACTIVE_GUILD === g.id ? " active" : "");
      item.style.animationDelay = delayFor(i) + "ms";
      var img = iconUrl(g);
      item.innerHTML = img
        ? '<img class="g-icon" src="' + img + '" onerror="this.remove()" alt="">' +
          '<div class="g-meta"><b>' + esc(g.name) + "</b><span>" + esc(g.id) + "</span></div>"
        : '<div class="g-icon ph">' + esc((g.name || "?").charAt(0).toUpperCase()) + "</div>" +
          '<div class="g-meta"><b>' + esc(g.name) + "</b><span>" + esc(g.id) + "</span></div>";
      item.addEventListener("click", function () {
        selectGuild(g.id);
      });
      list.appendChild(item);
    });
  }

  function selectGuild(gid) {
    ACTIVE_GUILD = gid;
    renderGuilds();
    var g = GUILDS.find(function (x) {
      return x.id === gid;
    });
    var title = $("#chan-title");
    if (title) title.innerHTML = "&Sigma; " + esc(g ? g.name : "?");
    var box = $("#channel-list");
    box.innerHTML = '<div class="empty"><span class="spinner"></span> Đang tải kênh...</div>';
    api("/api/guilds/" + gid + "/channels").then(function (r) {
      if (!r.ok) {
        box.innerHTML = '<div class="empty">' + esc(r.error || "Lỗi") + "</div>";
        return;
      }
      box.innerHTML = "";
      if (!r.channels.length) {
        box.innerHTML = '<div class="empty">Server này không có kênh voice</div>';
        return;
      }
      r.channels.forEach(function (c, i) {
        var item = document.createElement("div");
        item.className = "chan-item";
        item.style.animationDelay = delayFor(i) + "ms";
        var key = gid + ":" + c.id;
        var isActive = isHanging(gid, c.id);
        item.innerHTML =
          '<div class="c-icon">&#127908;</div>' +
          '<div class="c-meta"><b>' + esc(c.name) + "</b><span>Kênh voice</span></div>" +
          '<button type="button" class="btn-join' + (isActive ? " active" : "") + '">' +
          (isActive ? "&#10003; Đang treo" : "Treo voice") +
          "</button>";
        var btn = item.querySelector(".btn-join");
        btn.addEventListener("click", function () {
          if (isHanging(gid, c.id)) return stopHang(key);
          btn.disabled = true;
          btn.textContent = "Đang treo...";
          api("/api/hang", "POST", {
            guild_id: gid,
            channel_id: c.id,
            guild_name: g.name,
            channel_name: c.name,
          }).then(function (rr) {
            btn.disabled = false;
            if (!rr.ok) {
              btn.textContent = "Treo voice";
              return toast(rr.error || "Không thể treo voice", "err");
            }
            HANGS[key] = {
              started_at: rr.started_at,
              guild_id: gid,
              channel_id: c.id,
              guild_name: g.name,
              channel_name: c.name,
            };
            startTimers();
            toast("Đã treo voice vào " + c.name, "ok");
            selectGuild(gid);
          });
        });
        box.appendChild(item);
      });
    });
  }

  function stopHang(key) {
    var h = HANGS[key];
    if (!h) return;
    if (XAMIC[key]) {
      api("/api/xamic", "POST", { action: "stop", guild_id: h.guild_id, channel_id: h.channel_id });
      delete XAMIC[key];
    }
    api("/api/stop", "POST", { guild_id: h.guild_id, channel_id: h.channel_id }).then(function () {
      delete HANGS[key];
      renderHangPanel();
      toast("Đã ngừng treo voice", "ok");
      if (ACTIVE_GUILD) selectGuild(ACTIVE_GUILD);
    });
  }

  function stopAllHangs() {
    api("/api/stop", "POST", {}).then(function () {
      HANGS = {};
      XAMIC = {};
      renderHangPanel();
      toast("Đã ngừng tất cả voice", "ok");
      if (ACTIVE_GUILD) selectGuild(ACTIVE_GUILD);
    });
  }

  function loadDashboard() {
    api("/api/guilds").then(function (r) {
      if (!r.ok) return logout();
      GUILDS = r.guilds;
      $("#view-dash").innerHTML = dashShell();
      renderUserChip();
      renderGuilds();

      $("#guild-search").addEventListener("input", function (e) {
        GUILD_FILTER = e.target.value;
        renderGuilds();
      });
      $("#btn-logout").addEventListener("click", logout);
      $("#btn-stop-all").addEventListener("click", stopAllHangs);
      $("#btn-quest-toggle").addEventListener("click", toggleQuest);

      api("/api/status").then(function (st) {
        if (st.ok && st.hangs && st.hangs.length) {
          HANGS = {};
          XAMIC = {};
          st.hangs.forEach(function (h) {
            HANGS[h.key] = h;
            if (h.xamic) XAMIC[h.key] = true;
          });
          startTimers();
          toast("Đã nối lại phiên treo voice", "ok");
        }
      });

      refreshQuests();
      clearInterval(QUEST_POLL);
      QUEST_POLL = setInterval(refreshQuests, 5000);
      showView("dash");
    });
  }

  /* ---------------- quest ---------------- */
  function refreshQuests() {
    Promise.all([api("/api/quests"), api("/api/quests/status")]).then(function (res) {
      var r = res[0];
      var s = res[1];
      if (!r.ok || !s.ok) return;
      renderQuestToggle(!!r.running, s.auto_accept);
      renderQuestList(r.quests || []);
      renderQuestLog(s.logs || []);
    }).catch(function () {});
  }

  function renderQuestToggle(running, autoAccept) {
    var sw = $("#quest-auto-accept");
    if (sw) sw.checked = autoAccept !== false;
    var btn = $("#btn-quest-toggle");
    if (!btn) return;
    btn.textContent = running ? "Tắt Auto Quest" : "Bật Auto Quest";
    btn.classList.toggle("active", running);
    btn.disabled = false;
    var qr = $("#quest-running");
    qr.textContent = running ? "Đang chạy" : "Đang tắt";
    qr.className = "qstate " + (running ? "run" : "idle");
  }

  function renderQuestList(quests) {
    var box = $("#quest-list");
    if (!box) return;
    $("#quest-count").textContent = quests.length;
    if (!quests.length) {
      box.innerHTML = '<div class="empty">Không có quest nào đang hoạt động</div>';
      return;
    }
    box.innerHTML = "";
    quests.forEach(function (q, i) {
      var el = document.createElement("div");
      el.className = "quest-item";
      el.style.animationDelay = Math.min(i, 10) * 0.05 + "s";
      var pct = q.target > 0 ? Math.min(100, Math.round((q.value / q.target) * 100)) : 0;
      var st = q.completed
        ? ["done", "Hoàn thành"]
        : q.enrolled
          ? ["run", "Đang chạy"]
          : ["idle", "Chưa nhận"];
      el.innerHTML =
        '<div class="q-info"><b class="q-name">' + esc(q.name) + "</b><span class=\"q-app\">" + esc(q.app) + "</span></div>" +
        '<span class="q-badge">' + (TASK_LABEL[q.task] || esc(q.task || "?")) + "</span>" +
        '<div class="q-bar"><div class="q-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="q-prog">' + (q.target ? Math.min(q.value, q.target) + "/" + q.target + "s" : "") + "</div>" +
        '<span class="q-status ' + st[0] + '">' + st[1] + "</span>";
      box.appendChild(el);
    });
  }

  function renderQuestLog(logs) {
    var el = $("#quest-log");
    if (!el) return;
    if (!logs.length) {
      el.textContent = "Chưa có nhật ký. Bật Auto Quest để bắt đầu.";
      return;
    }
    var nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    el.textContent = logs.slice(-150).join("\n");
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }

  function toggleQuest() {
    var btn = $("#btn-quest-toggle");
    btn.disabled = true;
    var running = btn.classList.contains("active");
    api(running ? "/api/quests/stop" : "/api/quests/start", "POST", {
      auto_accept: $("#quest-auto-accept").checked,
    }).then(function (r) {
      if (!r.ok) {
        btn.disabled = false;
        return toast(r.error || "Thao tác thất bại", "err");
      }
      toast(running ? "Đã tắt Auto Quest" : "Đã bật Auto Quest", "ok");
      refreshQuests();
    });
  }

  /* ---------------- khác ---------------- */
  function logout() {
    api("/api/logout", "POST", {}).then(function () {
      clearInterval(QUEST_POLL);
      try {
        localStorage.removeItem("wummi_token");
      } catch (e) {}
      location.reload();
    });
  }

  function tickClock() {
    var d = new Date();
    var c = $("#clock");
    var dt = $("#date");
    if (c)
      c.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()].map(function (x) {
        return String(x).padStart(2, "0");
      }).join(":");
    if (dt)
      dt.textContent = d.toLocaleDateString("vi-VN", {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      });
  }

  /* ---------------- starfield ---------------- */
  var cv = $("#bg");
  var ctx = cv.getContext("2d");
  var P = [];
  var DPR = Math.min(2, window.devicePixelRatio || 1);
  var raf = null;

  function resize() {
    cv.width = innerWidth * DPR;
    cv.height = innerHeight * DPR;
    cv.style.width = innerWidth + "px";
    cv.style.height = innerHeight + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    var n = Math.max(40, Math.min(130, Math.round((innerWidth * innerHeight) / 16000)));
    P = [];
    for (var i = 0; i < n; i++) P.push(mkP());
  }

  function mkP() {
    return {
      x: Math.random() * innerWidth,
      y: Math.random() * innerHeight,
      r: Math.random() * 2.2 + 0.4,
      v: Math.random() * 0.4 + 0.1,
      a: Math.random() * 0.5 + 0.12,
      tw: Math.random() * Math.PI * 2,
    };
  }

  function anim() {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (var i = 0; i < P.length; i++) {
      var p = P[i];
      p.y -= p.v;
      p.tw += 0.02;
      if (p.y < -5) {
        p.y = innerHeight + 5;
        p.x = Math.random() * innerWidth;
      }
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, 7);
      ctx.fillStyle = "rgba(167,139,250," + p.a * (0.6 + 0.4 * Math.sin(p.tw)).toFixed(3) + ")";
      ctx.fill();
    }
    raf = requestAnimationFrame(anim);
  }

  function stopAnim() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopAnim();
    else if (!raf) anim();
  });

  /* ---------------- init ---------------- */
  function init() {
    tickClock();
    setInterval(tickClock, 1000);

    addEventListener("resize", function () {
      resize();
    });
    resize();
    anim();

    $("#btn-login").addEventListener("click", doLogin);
    $("#token-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") doLogin();
    });

    if (TOKEN) {
      api("/api/status").then(function (st) {
        if (st.ok && st.user) {
          USER = st.user;
          loadDashboard();
        } else {
          try {
            localStorage.removeItem("wummi_token");
          } catch (e) {}
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();