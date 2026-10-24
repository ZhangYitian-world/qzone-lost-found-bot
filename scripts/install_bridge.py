"""Pin candidate upstream source and patch only local logging / browser launch defaults."""
import os
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA = "e2c6da0ffea8937adac690352647bb023c312759"
target = ROOT / "data" / "bridge"
if not target.exists():
    subprocess.run(["git", "clone", "https://github.com/Gu-Heping/onebot-qzone.git", str(target)], check=True)
actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
if actual != SHA:
    subprocess.run(["git", "checkout", "--detach", SHA], cwd=target, check=True)
lock = ROOT / "scripts" / "bridge-package-lock.json"
if lock.exists():
    shutil.copyfile(lock, target / "package-lock.json")
subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=target, check=True)
# The upstream helper uses --no-sandbox by default. Do not weaken the local Chrome sandbox.
helper = target / "src" / "qzone" / "playwrightHelper.ts"
text = helper.read_text().replace("args: ['--no-sandbox', '--disable-setuid-sandbox'],", "args: [],")
helper.write_text(text)
# Raw upstream debug messages include request URLs. Use our facade's fixed status messages instead.
utils = target / "src" / "qzone" / "utils.ts"
text = utils.read_text()
start = text.index("export function log(")
end = text.index("\n}", start) + 2
utils.write_text(text[:start] + "export function log(_level: string, _msg: string): void {}" + text[end:])
# Keep every parsed entry. The upstream cap can discard a visible extra/pinned row
# while advancing start by count. Overlap is handled by our stable-ID deduplication.
client = target / "src" / "qzone" / "client.ts"
client.write_text(client.read_text().replace("const msglist = filtered.slice(0, count);", "const msglist = filtered;"))
# Do not put raw private responses into ordinary application logs.
raw_logger = target / "src" / "qzone" / "rawLogger.ts"
text = raw_logger.read_text()
text = text.replace("  return;\n  ensureLogDir();", "  ensureLogDir();")
if "QZONE_ALLOW_PRIVATE_RAW_DUMPS" not in text:
    text = text.replace("  ensureLogDir();", "  if (process.env['QZONE_ALLOW_PRIVATE_RAW_DUMPS'] !== '1') return;\n  ensureLogDir();")
raw_logger.write_text(text)
token_path = ROOT / "data" / "bridge-token"
if not token_path.exists():
    token_path.write_text(secrets.token_urlsafe(32))
token_path.chmod(0o600)
env_path = target / ".env"
if not env_path.exists():
    env_path.write_text("ONEBOT_ACCESS_TOKEN=" + token_path.read_text().strip() + "\n"
                        "QZONE_PLAYWRIGHT_CHANNEL=chrome\nQZONE_PLAYWRIGHT_HEADLESS=0\n")
env_path.chmod(0o600)
session = ROOT / "data" / "bridge-session"
session.mkdir(exist_ok=True, mode=0o700)
os.chmod(session, 0o700)
print("已准备固定版本的只读桥接；登录：data/bridge/node_modules/.bin/tsx scripts/bridge-server.mjs --login")
print("启动：data/bridge/node_modules/.bin/tsx scripts/bridge-server.mjs")
