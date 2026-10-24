"""Local command-line operations without requiring the management page."""
import argparse
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="QQ 空间失物 bot 命令行")
parser.add_argument("--url", default="http://127.0.0.1:8766")
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("status", help="来源、任务及预算状态")
collect = sub.add_parser("collect", help="立即触发来源采集")
collect.add_argument("source", choices=["wall", "rights", "all"])
imp = sub.add_parser("import", help="导入 JSON 样本")
imp.add_argument("path", type=Path)
exp = sub.add_parser("export", help="导出全部交接消息，不改变确认状态")
exp.add_argument("path", type=Path)
sub.add_parser("tasks", help="待处理与异常任务")
retry = sub.add_parser("retry", help="手动重试内容任务")
retry.add_argument("task_id", type=int)
args = parser.parse_args()
token = dotenv_values(ROOT / ".env").get("ADMIN_TOKEN")
with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=60,
                  trust_env=False) as client:
    def call(method, path, **kwargs):
        r = client.request(method, path, **kwargs)
        if r.is_error:
            raise SystemExit(f"请求未完成：HTTP {r.status_code}；请查看服务状态")
        return r.json()

    if args.command in ("status", "tasks"):
        data = call("GET", "/api/" + args.command)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.command == "collect":
        for sid in (["wall", "rights"] if args.source == "all" else [args.source]):
            call("POST", f"/api/sources/{sid}/collect")
            print(f"{sid} 已加入采集队列")
    elif args.command == "import":
        data = call("POST", "/api/import", json=json.loads(args.path.read_text()))
        print(f"新增 {data['new']}，更新 {data['updated']}，未变 {data['unchanged']}")
    elif args.command == "retry":
        call("POST", f"/api/tasks/{args.task_id}/retry")
        print("已重新排队")
    elif args.command == "export":
        cursor, messages = 0, []
        while True:
            page = call("GET", "/api/handoffs/export", params={"after": cursor, "limit": 500})
            messages.extend(page["messages"])
            if len(page["messages"]) < 500:
                break
            cursor = page["next_cursor"]
        args.path.write_text(json.dumps({"schema_version": "1.0", "messages": messages}, ensure_ascii=False, indent=2))
        args.path.chmod(0o600)
        print(f"已导出 {len(messages)} 条消息，未改变接收确认状态")
