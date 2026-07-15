import configparser
import datetime as dt
import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "stats_config.ini")
DB_DEFAULT = os.path.join(BASE_DIR, "stats.db")

DEFAULTS = {
    "api_token": "",
    "creator": "",
    "poll_seconds": "3600",
    "host": "0.0.0.0",
    "port": "8787",
    "db_path": DB_DEFAULT,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
latest_account_info = {
    "authenticated": False,
    "message": "Not checked yet",
}
latest_account_lock = threading.Lock()


def load_config():
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        config["Stats"] = DEFAULTS
        with open(CONFIG_FILE, "w") as configfile:
            config.write(configfile)
    else:
        config.read(CONFIG_FILE)
        if "Stats" not in config:
            config["Stats"] = DEFAULTS
        for key, value in DEFAULTS.items():
            if key not in config["Stats"]:
                config["Stats"][key] = value
        with open(CONFIG_FILE, "w") as configfile:
            config.write(configfile)

    cfg = dict(config["Stats"])
    cfg["api_token_from_env"] = "CHUB_API_TOKEN" in os.environ
    cfg["api_token"] = os.getenv("CHUB_API_TOKEN", cfg.get("api_token", "")).strip()
    cfg["creator"] = os.getenv("CHUB_CREATOR", cfg.get("creator", "")).strip()
    cfg["poll_seconds"] = int(os.getenv("CHUB_POLL_SECONDS", cfg.get("poll_seconds", "3600")))
    cfg["host"] = os.getenv("CHUB_HOST", cfg.get("host", "0.0.0.0"))
    cfg["port"] = int(os.getenv("CHUB_PORT", cfg.get("port", "8787")))
    cfg["db_path"] = os.getenv("CHUB_DB_PATH", cfg.get("db_path", DB_DEFAULT))
    return cfg


def save_api_token(api_token):
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    if "Stats" not in config:
        config["Stats"] = DEFAULTS
    config["Stats"]["api_token"] = api_token
    with open(CONFIG_FILE, "w") as configfile:
        config.write(configfile)


def get_api_headers(api_token):
    headers = {
        "accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                name TEXT,
                full_path TEXT,
                avatar_url TEXT,
                created_at TEXT
            )
            """
        )
        try:
            conn.execute("ALTER TABLE cards ADD COLUMN created_at TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                downloads INTEGER,
                forks INTEGER,
                rating REAL,
                rating_count INTEGER,
                chats INTEGER,
                messages INTEGER,
                favorites INTEGER,
                FOREIGN KEY(card_id) REFERENCES cards(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_card_ts ON stats(card_id, ts)")


def build_creator_query(creator):
    creator = creator.strip()
    if not creator:
        raise ValueError("Creator is required. Set it in stats_config.ini or CHUB_CREATOR.")
    if creator.isdigit():
        return {"creator": creator, "search": ""}, None
    logger.info("Creator looks like a username; using fullPath prefix search.")
    return {"search": f"{creator}/"}, creator.lower() + "/"


def fetch_user_projects(api_token, creator):
    headers = get_api_headers(api_token)
    url = f"https://api.chub.ai/api/users/{creator}"
    params = {
        "nsfw": "true",
        "nsfl": "true",
        "exclude_mine": "false",
        "include_projects": "true",
    }
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    projects = payload.get("projects", {})
    nodes = projects.get("nodes", [])
    if not isinstance(nodes, list):
        return []
    logger.info("Fetched %d projects for user %s.", len(nodes), creator)
    return nodes


def fetch_cards_via_search(api_token, creator):
    headers = get_api_headers(api_token)
    base_url = "https://api.chub.ai/search"
    all_nodes = []
    page = 1
    first = 50
    query_params, full_path_prefix = build_creator_query(creator)
    use_count = full_path_prefix is None
    while True:
        params = {
            "page": page,
            "first": first,
            "nsfw": "true",
            "nsfl": "true",
            "count": "true",
        }
        params.update(query_params)
        response = requests.get(base_url, headers=headers, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json().get("data", {})
        raw_nodes = payload.get("nodes", [])
        nodes = raw_nodes
        if full_path_prefix:
            nodes = [
                node for node in raw_nodes
                if (node.get("fullPath") or node.get("path") or "").lower().startswith(full_path_prefix)
            ]
        all_nodes.extend(nodes)
        count = payload.get("count", len(all_nodes))
        if not raw_nodes:
            break
        if use_count and len(all_nodes) >= count:
            break
        page += 1
        time.sleep(0.1)
    return all_nodes


def fetch_all_cards(api_token, creator):
    if creator and not creator.strip().isdigit():
        try:
            nodes = fetch_user_projects(api_token, creator.strip())
            if nodes:
                return nodes
            logger.warning("User projects endpoint returned no cards; falling back to search.")
        except Exception as exc:
            logger.warning("User projects fetch failed; falling back to search: %s", exc)
    return fetch_cards_via_search(api_token, creator)


def fetch_account_info(api_token):
    if not api_token:
        return {
            "authenticated": False,
            "message": "No token configured",
        }
    response = requests.get(
        "https://api.chub.ai/api/self",
        headers=get_api_headers(api_token),
        timeout=15,
    )
    if response.status_code != 200:
        return {
            "authenticated": False,
            "status": response.status_code,
            "message": response.text[:200],
        }
    payload = response.json()
    return {
        "authenticated": True,
        "id": payload.get("id"),
        "user_name": payload.get("user_name"),
        "name": payload.get("name"),
    }


def update_latest_account_info(api_token):
    global latest_account_info
    account = fetch_account_info(api_token)
    account["checked_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with latest_account_lock:
        latest_account_info = account
    if account.get("authenticated"):
        logger.info(
            "Token check OK: id=%s user_name=%s",
            account.get("id"),
            account.get("user_name") or account.get("name"),
        )
    else:
        logger.warning("Token check failed: %s", account.get("message", "unknown error"))
    return account


def get_latest_account_info():
    with latest_account_lock:
        return dict(latest_account_info)


def persist_snapshot(db_path, nodes):
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with sqlite3.connect(db_path) as conn:
        for node in nodes:
            card_id = node.get("id")
            if not card_id:
                continue
            conn.execute(
                """
                INSERT INTO cards (id, name, full_path, avatar_url, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    full_path=excluded.full_path,
                    avatar_url=excluded.avatar_url,
                    created_at=excluded.created_at
                """,
                (
                    card_id,
                    node.get("name", ""),
                    node.get("fullPath", node.get("path", "")),
                    node.get("avatar_url", ""),
                    node.get("createdAt", ""),
                ),
            )
            conn.execute(
                """
                INSERT INTO stats (
                    card_id, ts, downloads, forks, rating, rating_count,
                    chats, messages, favorites
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    now,
                    int(node.get("starCount") or 0),
                    int(node.get("forksCount") or 0),
                    float(node.get("rating") or 0),
                    int(node.get("ratingCount") or 0),
                    int(node.get("nChats") or 0),
                    int(node.get("nMessages") or 0),
                    int(node.get("n_favorites") or 0),
                ),
            )


def run_snapshot(cfg):
    update_latest_account_info(cfg["api_token"])
    nodes = fetch_all_cards(cfg["api_token"], cfg["creator"])
    persist_snapshot(cfg["db_path"], nodes)
    logger.info("Saved snapshot for %d cards.", len(nodes))
    return len(nodes)


def seconds_until_next_hour():
    now = dt.datetime.utcnow()
    next_hour = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return max(1, int((next_hour - now).total_seconds()))


def poll_loop(cfg, stop_event):
    align_to_hour = cfg["poll_seconds"] >= 3600
    while not stop_event.is_set():
        try:
            run_snapshot(cfg)
        except Exception as exc:
            logger.error("Failed to fetch stats: %s", exc)
        wait_seconds = seconds_until_next_hour() if align_to_hour else cfg["poll_seconds"]
        stop_event.wait(wait_seconds)


def json_response(handler, payload, status=200):
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def html_response(handler, content, status=200):
    data = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def get_cards_overview(db_path, window_hours=24):
    since = (dt.datetime.utcnow() - dt.timedelta(hours=window_hours)).isoformat() + "Z"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.full_path, c.avatar_url, c.created_at,
                   s.ts, s.downloads, s.forks, s.rating, s.rating_count,
                   s.chats, s.messages, s.favorites,
                   (
                       SELECT downloads FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS downloads_24h,
                   (
                       SELECT forks FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS forks_24h,
                   (
                       SELECT rating FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS rating_24h,
                   (
                       SELECT rating_count FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS rating_count_24h,
                   (
                       SELECT chats FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS chats_24h,
                   (
                       SELECT messages FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS messages_24h,
                   (
                       SELECT favorites FROM stats
                       WHERE card_id = c.id AND ts >= ?
                       ORDER BY ts ASC
                       LIMIT 1
                   ) AS favorites_24h
            FROM cards c
            LEFT JOIN stats s ON s.id = (
                SELECT id FROM stats
                WHERE card_id = c.id
                ORDER BY ts DESC
                LIMIT 1
            )
            ORDER BY datetime(c.created_at) DESC, c.name COLLATE NOCASE
            """,
            (since, since, since, since, since, since, since),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        def delta_field(field):
            latest = item.get(field)
            base = item.get(f"{field}_24h")
            if latest is None or base is None:
                return None
            return latest - base
        item["downloads_delta"] = delta_field("downloads")
        item["forks_delta"] = delta_field("forks")
        item["rating_delta"] = delta_field("rating")
        item["rating_count_delta"] = delta_field("rating_count")
        item["chats_delta"] = delta_field("chats")
        item["messages_delta"] = delta_field("messages")
        item["favorites_delta"] = delta_field("favorites")
        results.append(item)
    return results


def parse_ts(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def normalize_hour(value):
    return value.replace(minute=0, second=0, microsecond=0)


def format_ts(value):
    return value.replace(microsecond=0).isoformat() + "Z"


def get_card_timeseries(db_path, card_id, days, interval_seconds):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if days:
            since = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat() + "Z"
            rows = conn.execute(
                """
                SELECT ts, downloads, forks, rating, rating_count, chats, messages, favorites
                FROM stats
                WHERE card_id = ? AND ts >= ?
                ORDER BY ts ASC
                """,
                (card_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ts, downloads, forks, rating, rating_count, chats, messages, favorites
                FROM stats
                WHERE card_id = ?
                ORDER BY ts ASC
                """,
                (card_id,),
            ).fetchall()
    raw = [dict(row) for row in rows]
    logger.info("Timeseries rows for card %s: %d", card_id, len(raw))
    if not raw:
        return raw
    # Bucket to the hour so near-hour timestamps don't collapse into one point.
    points = {}
    for row in raw:
        ts_obj = parse_ts(row.get("ts"))
        if not ts_obj:
            continue
        hour = normalize_hour(ts_obj)
        key = format_ts(hour)
        existing = points.get(key)
        if not existing or ts_obj > parse_ts(existing.get("_orig_ts")):
            row["_orig_ts"] = row.get("ts")
            row["ts"] = key
            points[key] = row
    data = list(points.values())
    if not data:
        return data
    interval = max(60, int(interval_seconds or 3600))
    data.sort(key=lambda row: row["ts"])
    start_ts = parse_ts(data[0]["ts"])
    end_ts = parse_ts(data[-1]["ts"])
    if not start_ts or not end_ts:
        return data
    points = {row["ts"]: row for row in data}
    filled = []
    current = start_ts
    while current <= end_ts:
        ts = format_ts(current)
        row = points.get(ts)
        if row:
            filled.append(row)
        else:
            filled.append(
                {
                    "ts": ts,
                    "downloads": None,
                    "forks": None,
                    "rating": None,
                    "rating_count": None,
                    "chats": None,
                    "messages": None,
                    "favorites": None,
                }
            )
        current += dt.timedelta(seconds=interval)
    return filled


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Chub Card Stats</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f5f6f8; }
    h1 { margin-bottom: 8px; }
    .header-row { display: flex; align-items: baseline; gap: 8px; }
    .total-count { color: #555; font-size: 14px; }
    .account-info { color: #555; font-size: 14px; }
    .token-form { display: flex; gap: 6px; align-items: center; margin: 8px 0 12px; flex-wrap: wrap; }
    .token-form input { min-width: 320px; padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 6px; }
    .token-status { color: #555; font-size: 13px; }
    .global-controls { margin: 8px 0 16px; display: flex; gap: 8px; flex-wrap: wrap; }
    .btn { border: 0; background: #1f2937; color: #fff; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
    .btn-secondary { background: #6b7280; }
    #cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(600px, 1fr)); gap: 16px; }
    .card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .header { display: flex; gap: 16px; align-items: center; }
    .avatar { width: 72px; height: 72px; border-radius: 50%; object-fit: cover; background: #eee; }
    .name { font-size: 18px; font-weight: bold; }
    .path { color: #666; }
    .stats { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 8px; margin: 12px 0; }
    .stat { background: #f0f3f7; padding: 8px 10px; border-radius: 6px; }
    .card-controls { display: flex; gap: 8px; margin-top: 8px; }
    .chart-wrap { margin-top: 8px; display: none; }
    .chart-wrap.active { display: block; }
    .chart-area { height: 260px; position: relative; }
    .empty-chart { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; color: #6b7280; font-size: 14px; }
    .chart-tabs { display: flex; gap: 8px; margin-top: 8px; }
    .chart-tabs button { background: #e5e7eb; border: 0; padding: 6px 10px; border-radius: 6px; cursor: pointer; }
    .chart-tabs button.active { background: #1f2937; color: #fff; }
    canvas { width: 100%; height: 100%; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
</head>
<body>
  <div class="header-row">
    <h1>Chub Card Stats</h1>
    <div class="total-count" id="total-count"></div>
    <div class="account-info" id="account-info"></div>
  </div>
  <div class="token-form">
    <input id="token-input" type="password" placeholder="Paste new Chub session token"/>
    <button class="btn btn-secondary" id="save-token">Save token</button>
    <span class="token-status" id="token-status"></span>
  </div>
  <div class="global-controls">
    <button class="btn" id="show-all">Show all charts</button>
    <button class="btn btn-secondary" id="hide-all">Hide all charts</button>
    <button class="btn btn-secondary" data-global-range="1h">Last 1 hour</button>
    <button class="btn btn-secondary" data-global-range="1d">Last 1 day</button>
    <button class="btn btn-secondary" data-global-range="7">Last 7 days</button>
    <button class="btn btn-secondary" data-global-range="30">Last 30 days</button>
    <button class="btn btn-secondary" data-global-range="all">All time</button>
    <label class="btn btn-secondary" style="background:#e5e7eb;color:#111827;">
      Custom:
      <input id="custom-value" type="number" min="1" value="7" style="width:70px;margin-left:6px;" />
      <select id="custom-unit" style="margin-left:6px;">
        <option value="hours">hours</option>
        <option value="days" selected>days</option>
        <option value="weeks">weeks</option>
        <option value="months">months</option>
        <option value="years">years</option>
      </select>
      <button class="btn btn-secondary" id="apply-custom" style="margin-left:6px;">Apply</button>
    </label>
    <label class="btn btn-secondary" style="background:#e5e7eb;color:#111827;">
      Sort:
      <select id="sort-mode" style="margin-left:6px;">
        <option value="newest">Newest</option>
        <option value="messages_24h">Messages (range)</option>
        <option value="chats_24h">Chats (range)</option>
        <option value="downloads_24h">Downloads (range)</option>
        <option value="rating_count_24h">Rating Count (range)</option>
      </select>
    </label>
  </div>
  <div id="cards"></div>
  <script>
    let globalRange = '1d';

    async function fetchCards(windowHours) {
      const suffix = windowHours ? `?window_hours=${windowHours}` : '';
      const res = await fetch(`/api/cards${suffix}`, { cache: 'no-store' });
      return res.json();
    }

    async function fetchAccountInfo() {
      const res = await fetch(`/api/self?_ts=${Date.now()}`, { cache: 'no-store' });
      return res.json();
    }

    async function saveToken() {
      const input = document.getElementById('token-input');
      const status = document.getElementById('token-status');
      const token = input.value.trim();
      if (!token) {
        status.textContent = 'Paste a token first.';
        return;
      }
      status.textContent = 'Checking token...';
      const res = await fetch('/api/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token })
      });
      const account = await res.json();
      renderAccountInfo(account);
      if (account.authenticated) {
        input.value = '';
        status.textContent = account.persisted
          ? 'Token saved and verified.'
          : 'Token verified for this running container. Update Docker env to persist after restart.';
      } else {
        status.textContent = `Token check failed${account.status ? ` (${account.status})` : ''}.`;
      }
    }

    function renderAccountInfo(account) {
      const el = document.getElementById('account-info');
      if (!account.authenticated) {
        el.textContent = '(Token: not verified)';
        return;
      }
      el.textContent = `(ID: ${account.id ?? '-'} | User_Name: ${account.user_name ?? account.name ?? '-'})`;
    }

    async function fetchSeries(cardId, windowHours) {
      const suffix = windowHours ? `?window_hours=${windowHours}` : '';
      const cacheBust = `${suffix ? '&' : '?'}_ts=${Date.now()}`;
      const res = await fetch(`/api/stats/${cardId}${suffix}${cacheBust}`, { cache: 'no-store' });
      return res.json();
    }

    function formatDelta(value) {
      if (value === null || value === undefined) return '';
      const sign = value > 0 ? '+' : '';
      const rounded = Number.isInteger(value) ? value : value.toFixed(2);
      return ` ${sign}${rounded}`;
    }

    function renderCard(container, card) {
      const cardEl = document.createElement('div');
      cardEl.className = 'card';
      const cardId = card.id;
      cardEl.innerHTML = `
        <div class="header">
          <img class="avatar" src="${card.avatar_url || ''}" alt="avatar"/>
          <div>
            <div class="name">${card.name || 'Unknown'}</div>
            <div class="path">${card.full_path || ''}</div>
          </div>
        </div>
        <div class="stats">
          <div class="stat">💬 ${card.messages ?? '-'}${formatDelta(card.messages_delta)}</div>
          <div class="stat">🔖 ${card.chats ?? '-'}${formatDelta(card.chats_delta)}</div>
          <div class="stat">⬇️ ${card.downloads ?? '-'}${formatDelta(card.downloads_delta)}</div>
          <div class="stat">❤️ ${card.favorites ?? '-'}${formatDelta(card.favorites_delta)}</div>
          <div class="stat">🍴 ${card.forks ?? '-'}${formatDelta(card.forks_delta)}</div>
          <div class="stat">⭐ ${card.rating ?? '-'}${formatDelta(card.rating_delta)}</div>
          <div class="stat">📊 ${card.rating_count ?? '-'}${formatDelta(card.rating_count_delta)}</div>
          <div class="stat">🕒 ${card.ts ?? '-'}</div>
        </div>
        <div class="card-controls">
          <button class="btn btn-secondary toggle-charts" data-card="${cardId}">Show charts</button>
        </div>
        <div class="chart-wrap" id="charts-${cardId}">
          <div class="chart-tabs">
            <button data-range="1h" data-card="${cardId}" class="range-btn">Last 1 hour</button>
            <button data-range="1d" data-card="${cardId}" class="range-btn">Last 1 day</button>
            <button data-range="7" data-card="${cardId}" class="range-btn">Last 7 days</button>
            <button data-range="30" data-card="${cardId}" class="range-btn">Last 30 days</button>
            <button data-range="all" data-card="${cardId}" class="range-btn">All time</button>
          </div>
          <div class="chart-area">
            <div class="empty-chart" id="empty-${cardId}">No data yet</div>
            <canvas id="chart-${cardId}"></canvas>
          </div>
        </div>
      `;
      container.appendChild(cardEl);
      return cardEl;
    }

    function buildChart(ctx, series, xBounds, showTime, visibility) {
      const toNumber = value => {
        if (value === null || value === undefined) {
          return null;
        }
        const num = Number(value);
        return Number.isNaN(num) ? null : num;
      };
      const labels = series.map(row => row.ts);
      const dataPoints = metric => series.map(row => toNumber(row[metric]));
      return new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Downloads', data: dataPoints('downloads'), borderColor: '#3b82f6', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Downloads'] === false },
            { label: 'Forks', data: dataPoints('forks'), borderColor: '#6366f1', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Forks'] === false },
            { label: 'Chats', data: dataPoints('chats'), borderColor: '#10b981', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Chats'] === false },
            { label: 'Messages', data: dataPoints('messages'), borderColor: '#f59e0b', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Messages'] === false },
            { label: 'Favorites', data: dataPoints('favorites'), borderColor: '#ef4444', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Favorites'] === false },
            { label: 'Rating Count', data: dataPoints('rating_count'), borderColor: '#8b5cf6', yAxisID: 'counts', pointRadius: 2, hidden: visibility['Rating Count'] === false },
            { label: 'Rating', data: dataPoints('rating'), borderColor: '#0ea5e9', yAxisID: 'rating', pointRadius: 2, hidden: visibility['Rating'] === false }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          spanGaps: false,
          plugins: {
            legend: {
              onClick: (event, legendItem, legend) => {
                const chart = legend.chart;
                const label = legendItem.text;
                const newState = !(visibility[label] ?? true);
                visibility[label] = newState;
                chartInstances.forEach(instance => {
                  instance.data.datasets.forEach(ds => {
                    if (ds.label === label) {
                      ds.hidden = !newState;
                    }
                  });
                  instance.update();
                });
              }
            }
          },
          scales: {
            x: {
              type: 'time',
              min: xBounds?.min ?? undefined,
              max: xBounds?.max ?? undefined,
              time: { unit: showTime ? 'hour' : 'day' },
              ticks: { source: 'data' }
            },
            counts: { type: 'linear', position: 'left' },
            rating: { type: 'linear', position: 'right', min: 0, max: 5, grid: { drawOnChartArea: false } }
          }
        }
      });
    }

    async function ensureChart(cardId, rangeKey) {
      const days = rangeKey === 'all' ? null : Number(rangeKey);
      const series = await fetchSeries(cardId, globalWindowHours);
      const emptyLabel = document.getElementById(`empty-${cardId}`);
      if (!series.length) {
        if (emptyLabel) {
          emptyLabel.style.display = 'flex';
        }
        return null;
      }
      if (emptyLabel) {
        emptyLabel.style.display = 'none';
      }
      const hasValue = row => (
        row.downloads !== null ||
        row.forks !== null ||
        row.rating !== null ||
        row.rating_count !== null ||
        row.chats !== null ||
        row.messages !== null ||
        row.favorites !== null
      );
      const xValues = series
        .filter(hasValue)
        .map(row => Date.parse(row.ts))
        .filter(value => !Number.isNaN(value))
        .sort((a, b) => a - b);
      let xBounds = null;
      if (xValues.length) {
        let min = xValues[0];
        let max = xValues[xValues.length - 1];
        if (min === max) {
          min -= 60 * 60 * 1000;
          max += 60 * 60 * 1000;
        } else {
          const pad = Math.max(30 * 60 * 1000, Math.round((max - min) * 0.05));
          min -= pad;
          max += pad;
        }
        xBounds = { min, max };
      }
      const rangeMs = xBounds ? (xBounds.max - xBounds.min) : null;
      const showTime = rangeMs !== null && rangeMs <= 2 * 24 * 60 * 60 * 1000;
      const canvas = document.getElementById(`chart-${cardId}`);
      if (canvas.chartInstance) {
        canvas.chartInstance.destroy();
      }
      const chart = buildChart(canvas, series, xBounds, showTime, datasetVisibility);
      canvas.chartInstance = chart;
      chartInstances.push(chart);
      return chart;
    }

    function setActiveTab(cardId, rangeKey) {
      const tabs = document.querySelectorAll(`.range-btn[data-card="${cardId}"]`);
      tabs.forEach(tab => tab.classList.toggle('active', tab.dataset.range === rangeKey));
    }

    async function applyGlobalRange() {
      if (globalRange === '1h') globalWindowHours = 1;
      if (globalRange === '1d') globalWindowHours = 24;
      if (globalRange === '7') globalWindowHours = 24 * 7;
      if (globalRange === '30') globalWindowHours = 24 * 30;
      if (globalRange === 'all') globalWindowHours = null;
      if (globalRange === 'custom') {
        const value = Number(document.getElementById('custom-value').value);
        const unit = document.getElementById('custom-unit').value;
        const multipliers = { hours: 1, days: 24, weeks: 24 * 7, months: 24 * 30, years: 24 * 365 };
        globalWindowHours = Math.max(1, Math.round(value * multipliers[unit]));
      }
      await render();
      const wraps = document.querySelectorAll('.chart-wrap.active');
      for (const wrap of wraps) {
        const cardId = wrap.id.replace('charts-', '');
        setActiveTab(cardId, globalRange);
        await ensureChart(cardId, globalRange);
      }
    }

    function sortCards(cards, mode) {
      const score = value => (value === null || value === undefined ? -Infinity : value);
      const byDelta = key => (a, b) => score(b[key]) - score(a[key]);
      if (mode === 'messages_24h') return cards.sort(byDelta('messages_delta'));
      if (mode === 'chats_24h') return cards.sort(byDelta('chats_delta'));
      if (mode === 'downloads_24h') return cards.sort(byDelta('downloads_delta'));
      if (mode === 'rating_count_24h') return cards.sort(byDelta('rating_count_delta'));
      return cards.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    }

    function renderCards(container, cards, sortMode) {
      container.innerHTML = '';
      const sorted = sortCards([...cards], sortMode);
      for (const card of sorted) {
        renderCard(container, card);
      }
    }

    let globalWindowHours = 24;
    const chartInstances = [];
    const datasetVisibility = {};

    function resetCharts() {
      chartInstances.splice(0, chartInstances.length);
    }

    async function render() {
      const container = document.getElementById('cards');
      fetchAccountInfo().then(renderAccountInfo).catch(() => {
        document.getElementById('account-info').textContent = '(Token: check failed)';
      });
      const cards = await fetchCards(globalWindowHours);
      document.getElementById('total-count').textContent = `(${cards.length})`;
      if (!cards.length) {
        container.innerHTML = '<p>No cards found yet.</p>';
        return;
      }
      const sortSelect = document.getElementById('sort-mode');
      renderCards(container, cards, sortSelect.value);
      resetCharts();
      sortSelect.addEventListener('change', () => {
        renderCards(container, cards, sortSelect.value);
        resetCharts();
      });

      container.addEventListener('click', async (event) => {
        const target = event.target;
        if (target.classList.contains('toggle-charts')) {
          const cardId = target.dataset.card;
          const wrap = document.getElementById(`charts-${cardId}`);
          const isActive = wrap.classList.toggle('active');
          target.textContent = isActive ? 'Hide charts' : 'Show charts';
          if (isActive) {
            setActiveTab(cardId, globalRange);
            await ensureChart(cardId, globalRange);
          }
        }
        if (target.classList.contains('range-btn')) {
          globalRange = target.dataset.range;
          await applyGlobalRange();
        }
      });

      document.getElementById('show-all').addEventListener('click', async () => {
        const toggles = document.querySelectorAll('.toggle-charts');
        for (const toggle of toggles) {
          if (toggle.textContent !== 'Hide charts') {
            toggle.click();
          }
        }
      });

      document.getElementById('hide-all').addEventListener('click', () => {
        const wraps = document.querySelectorAll('.chart-wrap');
        wraps.forEach(wrap => wrap.classList.remove('active'));
        const toggles = document.querySelectorAll('.toggle-charts');
        toggles.forEach(t => t.textContent = 'Show charts');
      });

      document.querySelectorAll('[data-global-range]').forEach(button => {
        button.addEventListener('click', async () => {
          globalRange = button.dataset.globalRange;
          await applyGlobalRange();
        });
      });

      document.getElementById('apply-custom').addEventListener('click', () => {
        globalRange = 'custom';
        applyGlobalRange();
      });

      document.getElementById('save-token').addEventListener('click', saveToken);
      document.getElementById('token-input').addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          saveToken();
        }
      });
    }

    render();
  </script>
</body>
</html>
"""


class StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return html_response(self, HTML_PAGE)
        if parsed.path == "/api/cards":
            query = parse_qs(parsed.query)
            window_hours = query.get("window_hours", [24])[0]
            window_val = int(window_hours) if window_hours else 24
            cards = get_cards_overview(self.server.db_path, window_val)
            return json_response(self, cards)
        if parsed.path == "/api/self":
            account = get_latest_account_info()
            return json_response(self, account)
        if parsed.path.startswith("/api/stats/"):
            card_id = parsed.path.split("/api/stats/")[-1]
            query = parse_qs(parsed.query)
            window_hours = query.get("window_hours", [None])[0]
            window_val = int(window_hours) if window_hours else None
            days_val = None
            if window_val:
                days_val = max(1, (window_val + 23) // 24)
            series = get_card_timeseries(self.server.db_path, card_id, days_val, self.server.poll_seconds)
            return json_response(self, series)
        if parsed.path == "/api/health":
            return json_response(self, {"status": "ok"})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/token":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            token = payload.get("token", "").strip()
            if not token:
                return json_response(self, {"authenticated": False, "message": "Token is required"}, 400)

            self.server.cfg["api_token"] = token
            self.server.api_token = token
            account = update_latest_account_info(token)
            persisted = False
            if account.get("authenticated") and not self.server.cfg.get("api_token_from_env"):
                save_api_token(token)
                persisted = True
            account["persisted"] = persisted
            if account.get("authenticated"):
                try:
                    snapshot_count = run_snapshot(self.server.cfg)
                    account["snapshot_cards"] = snapshot_count
                except Exception as exc:
                    logger.error("Token updated but immediate snapshot failed: %s", exc)
                    account["snapshot_error"] = str(exc)
            return json_response(self, account)
        except Exception as exc:
            logger.error("Failed to update token: %s", exc)
            return json_response(self, {"authenticated": False, "message": str(exc)}, 500)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def run_server(cfg):
    server = HTTPServer((cfg["host"], cfg["port"]), StatsHandler)
    server.cfg = cfg
    server.db_path = cfg["db_path"]
    server.poll_seconds = cfg["poll_seconds"]
    server.api_token = cfg["api_token"]
    logger.info("Serving on http://%s:%s", cfg["host"], cfg["port"])
    server.serve_forever()


def main():
    cfg = load_config()
    init_db(cfg["db_path"])

    stop_event = threading.Event()
    poll_thread = threading.Thread(target=poll_loop, args=(cfg, stop_event), daemon=True)
    poll_thread.start()

    try:
        run_server(cfg)
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        stop_event.set()


if __name__ == "__main__":
    main()
