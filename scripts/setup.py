"""Create local credentials once; never print secrets or overwrite existing configuration."""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    print(".env 已存在，保留现有配置。")
else:
    template = (root / ".env.example").read_text()
    template = template.replace("ADMIN_TOKEN=\n", "ADMIN_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    template = template.replace("DOWNSTREAM_TOKEN=\n", "DOWNSTREAM_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(template)
    print("已创建 .env（仅当前用户可读写）；日预算 5 元，等待填写 DeepSeek API Key。")
(root / "data").mkdir(exist_ok=True, mode=0o700)
source_path = root / "data" / "local-config.json"
if not source_path.exists():
    fd = os.open(source_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write((root / "local-config.example.json").read_text())
    print("已创建 data/local-config.json，请在本机填写专用小号和两个来源账号。")
print("启动：.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8766 --workers 1 --no-access-log")
print("登录 http://127.0.0.1:8766，管理令牌见本机 .env 的 ADMIN_TOKEN。")
