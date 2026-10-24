"""Persist and acknowledge handoffs in a separate local DB. Never query people or send mail."""
import json
import sqlite3
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
token = dotenv_values(ROOT / ".env").get("DOWNSTREAM_TOKEN")
path = ROOT / "data" / "demo-consumer.sqlite3"
with sqlite3.connect(path) as db, httpx.Client(base_url="http://127.0.0.1:8766", timeout=30, trust_env=False,
        headers={"Authorization": "Bearer " + token}) as client:
    db.execute("CREATE TABLE IF NOT EXISTS inbox(idempotency_key TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS latest(event_id TEXT PRIMARY KEY,version INTEGER NOT NULL,action TEXT NOT NULL)")
    result = client.post("/api/downstream/pull")
    result.raise_for_status()
    received = stale = 0
    for message in result.json()["messages"]:
        p = message["payload"]
        latest = client.get(f"/api/downstream/events/{p['event_id']}/latest")
        latest.raise_for_status()
        if latest.json()["version"] != p["event_version"]:
            stale += 1
            continue
        with db:
            db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?)", (message["idempotency_key"],
                       json.dumps(p, ensure_ascii=False)))
            db.execute("INSERT INTO latest VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                       "version=excluded.version,action=excluded.action WHERE excluded.version>latest.version",
                       (p["event_id"], p["event_version"], p["event_action"]))
        ack = client.post(f"/api/downstream/messages/{message['message_id']}/ack")
        ack.raise_for_status()
        received += 1
    print(f"本地联调接收并确认 {received} 条，跳过过期版本 {stale} 条；未查询人员、未发送邮件。")
path.chmod(0o600)
