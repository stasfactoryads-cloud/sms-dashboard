#!/usr/bin/env python3
"""Дашборд sms.ru: баланс, отправки и расходы за сегодня.

  python3 sms_dashboard.py update            обновить данные (баланс и стоимость записанных sms_id)
  python3 sms_dashboard.py record ID [ID...] записать sms_id отправленных сообщений
  python3 sms_dashboard.py serve             страница на http://127.0.0.1:8765, обновление раз в сутки

Ключ API: переменная SMSRU_API_ID или файл .env рядом со скриптом.
"""

import datetime as dt
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DASHBOARD_FILE = DATA / "dashboard.json"
SENT_FILE = DATA / "sent_ids.json"
ENV_FILE = BASE / ".env"

API_URL = os.environ.get("SMSRU_API_URL", "https://sms.ru")
LOW_BALANCE = 5000
REFRESH_EVERY = 24 * 3600
PORT = 8765
MSK = dt.timezone(dt.timedelta(hours=3))
update_lock = threading.Lock()


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, payload):
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso():
    return dt.datetime.now(MSK).isoformat(timespec="seconds")


def api_post(method, **fields):
    api_id = os.environ.get("SMSRU_API_ID")
    if not api_id:
        raise RuntimeError("не задан SMSRU_API_ID (добавьте его в .env)")
    body = urllib.parse.urlencode({"api_id": api_id, "json": 1, **fields}).encode()
    try:
        with urllib.request.urlopen(f"{API_URL}/{method}", data=body, timeout=30) as resp:
            return json.load(resp)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"запрос к sms.ru не удался: {exc}") from exc


def record(sms_ids):
    sent = load_json(SENT_FILE, {})
    for sms_id in sms_ids:
        sent.setdefault(sms_id, {"sent_at": now_iso(), "cost": None})
    save_json(SENT_FILE, sent)
    print(f"Записано sms_id: {len(sms_ids)}")


def fetch_costs(sent):
    """Дозапрашивает стоимость у sms.ru для записей, где ее еще нет."""
    pending = [sms_id for sms_id, info in sent.items() if info["cost"] is None]
    for start in range(0, len(pending), 100):
        resp = api_post("sms/status", sms_id=",".join(pending[start:start + 100]))
        if resp.get("status") != "OK":
            raise RuntimeError(f"sms/status: код {resp.get('status_code')} {resp.get('status_text', '')}")
        for sms_id, info in resp.get("sms", {}).items():
            if sms_id not in sent:
                continue
            if "cost" in info:
                sent[sms_id]["cost"] = float(info["cost"])
            elif info.get("status") == "ERROR":
                sent[sms_id]["cost"] = 0.0


def build_dashboard(sent, balance):
    today = dt.datetime.now(MSK).date()
    by_day = {}
    for info in sent.values():
        day = dt.datetime.fromisoformat(info["sent_at"]).astimezone(MSK).date().isoformat()
        stat = by_day.setdefault(day, {"count": 0, "cost": 0.0})
        stat["count"] += 1
        stat["cost"] += info["cost"] or 0

    last_7_days = []
    for offset in range(6, -1, -1):
        day = (today - dt.timedelta(days=offset)).isoformat()
        stat = by_day.get(day, {"count": 0, "cost": 0.0})
        last_7_days.append({"date": day, "count": stat["count"], "cost": round(stat["cost"], 2)})

    return {
        "updated_at": now_iso(),
        "balance": balance,
        "low_balance_threshold": LOW_BALANCE,
        "low_balance": balance < LOW_BALANCE,
        "today": last_7_days[-1],
        "last_7_days": last_7_days,
        "pending_costs": sum(1 for info in sent.values() if info["cost"] is None),
    }


def update():
    with update_lock:
        balance_resp = api_post("my/balance")
        if balance_resp.get("status") != "OK":
            raise RuntimeError(f"my/balance: код {balance_resp.get('status_code')} {balance_resp.get('status_text', '')}")

        sent = load_json(SENT_FILE, {})
        fetch_costs(sent)
        save_json(SENT_FILE, sent)

        dashboard = build_dashboard(sent, float(balance_resp["balance"]))
        save_json(DASHBOARD_FILE, dashboard)
        today = dashboard["today"]
        print(f"Баланс {dashboard['balance']} руб. Сегодня: {today['count']} шт. на {today['cost']} руб.")
        return dashboard


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._reply(200, "text/html; charset=utf-8", (BASE / "index.html").read_bytes())
        elif self.path in ("/api/dashboard", "/data/dashboard.json"):
            try:
                self._json(200, load_json(DASHBOARD_FILE) or update())
            except RuntimeError as exc:
                self._json(502, {"error": str(exc)})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/refresh":
            return self.send_error(404)
        try:
            self._json(200, update())
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})

    def _json(self, status, payload):
        self._reply(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode())

    def _reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def scheduler():
    """Раз в час проверяет возраст данных и обновляет их, если прошли сутки."""
    while True:
        try:
            stale = not DASHBOARD_FILE.exists() or time.time() - DASHBOARD_FILE.stat().st_mtime > REFRESH_EVERY
            if stale:
                update()
        except RuntimeError as exc:
            print(f"Ошибка обновления: {exc}", file=sys.stderr)
        time.sleep(3600)


def serve():
    threading.Thread(target=scheduler, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Страница: http://127.0.0.1:{PORT}  (остановка: Ctrl+C)")
    server.serve_forever()


def main():
    load_env()
    args = sys.argv[1:]
    try:
        if args == ["update"]:
            update()
        elif args[:1] == ["record"] and len(args) > 1:
            record(args[1:])
        elif args == ["serve"]:
            serve()
        else:
            print(__doc__)
            sys.exit(1)
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
