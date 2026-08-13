// WebSocket client: live group/switch updates, connection status, auto-refresh.
(function () {
  "use strict";

  var accessKey = window.__ACCESS_KEY__ || "";
  var pageAutoRefresh = Number(window.__PAGE_AUTO_REFRESH__ || 0);
  var ws = null;

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function setConn(state) {
    // state: "live" | "offline" | "unauthorized"
    var el = document.getElementById("conn-status");
    if (!el) return;
    if (state === "unauthorized") {
      el.textContent = "unauthorized — add ?key= to the URL";
      el.className = "conn-offline";
    } else {
      el.textContent = state;
      el.className = state === "live" ? "conn-online" : "conn-offline";
    }
  }

  function stampRefresh() {
    var el = document.getElementById("last-refresh");
    if (!el) return;
    var now = new Date();
    el.textContent = now.toLocaleDateString() + " " + now.toTimeString().slice(0, 8);
  }

  // Update the group cards on the home page. No-ops on pages without cards.
  function applySnapshot(snapshot) {
    var groups = (snapshot && snapshot.groups) || {};
    Object.keys(groups).forEach(function (name) {
      var group = groups[name];
      var groupCard = document.querySelector('.card[data-group-id="' + cssEscape(group.id) + '"]');
      if (!groupCard) return;

      var stateBadge = groupCard.querySelector('.card-header [data-field="scheduled_state"]');
      if (stateBadge) {
        stateBadge.textContent = group.scheduled_state || "";
        stateBadge.className = "status-badge " + (group.scheduled_state === "ON" ? "on" : "off");
      }

      var nextEl = groupCard.querySelector('.card-header [data-field="next_change"]');
      if (nextEl) nextEl.textContent = group.next_change || "—";

      groupCard.querySelectorAll(".card-header .mode-button").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-mode") === group.mode);
      });

      var groupControlsMode = group.mode !== "auto";
      var switches = group.switches || {};
      Object.keys(switches).forEach(function (swName) {
        var sw = switches[swName];
        var row = groupCard.querySelector('tr[data-switch-id="' + cssEscape(sw.id) + '"]');
        if (!row) return;

        var indicator = row.querySelector('[data-field="indicator"]');
        if (indicator) {
          indicator.className =
            "status-indicator " + (sw.is_on ? "status-indicator--active" : "status-indicator--inactive");
        }

        var stateLabel = row.querySelector('[data-field="state"]');
        if (stateLabel) {
          stateLabel.textContent = sw.is_on ? "ON" : "OFF";
          stateLabel.className = "status-label " + (sw.is_on ? "on" : "off");
        }

        var modeGroup = row.querySelector('[data-field="mode-buttons"]');
        if (modeGroup) {
          modeGroup.classList.toggle("disabled", groupControlsMode);
          modeGroup.querySelectorAll(".mode-button").forEach(function (btn) {
            btn.disabled = groupControlsMode;
            btn.classList.toggle("active", btn.getAttribute("data-mode") === sw.mode);
          });
        }

        var reasonEl = row.querySelector('[data-field="reason"]');
        if (reasonEl) {
          reasonEl.textContent = sw.mode !== "auto" || sw.system_state ? sw.reason || "" : "";
        }
      });
    });
    stampRefresh();
  }

  function sendCommand(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn("WebSocket not connected");
      return;
    }
    ws.send(JSON.stringify(payload));
  }

  // Exposed for the inline onclick handlers in home.html.
  window.setGroupMode = function (btn, mode) {
    var card = btn.closest("[data-group-name]");
    if (!card) return;
    sendCommand({ type: "command", action: "set_group_mode", group_id: card.dataset.groupName, mode: mode });
  };

  window.setSwitchMode = function (btn, mode) {
    var row = btn.closest("[data-switch-name]");
    if (!row) return;
    sendCommand({ type: "command", action: "set_switch_mode", switch_id: row.dataset.switchName, mode: mode });
  };

  function connect() {
    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = protocol + "//" + window.location.host + "/ws";
    if (accessKey) url += "?key=" + encodeURIComponent(accessKey);
    ws = new WebSocket(url);

    ws.onopen = function () { setConn("live"); };
    ws.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "state_update" && msg.state) applySnapshot(msg.state);
    };
    ws.onclose = function (event) {
      ws = null;
      // 1008 = policy violation: the access key was missing/invalid. Retrying
      // with the same URL is futile, so stop instead of hammering the server.
      if (event && event.code === 1008) {
        setConn("unauthorized");
        return;
      }
      setConn("offline");
      setTimeout(connect, 2000);
    };
    ws.onerror = function () { if (ws) ws.close(); };
  }

  if (pageAutoRefresh > 0) {
    setInterval(function () { window.location.reload(); }, pageAutoRefresh * 1000);
  }
  connect();
})();
