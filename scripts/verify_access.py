"""Read-only two-source probe. --import-samples imports three explicitly chosen historical samples."""
import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import Bridge, normalize  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import dumps, iso  # noqa: E402
from app.service import content_digest  # noqa: E402

parser = argparse.ArgumentParser(description="实际账号分页与字段只读验证")
parser.add_argument("--import-samples", action="store_true")
args = parser.parse_args()
cfg = Settings()
b = Bridge(cfg)
report = {"at": iso(), "login": b.call("get_login_info", {})["user_id"], "sources": []}
if str(report["login"]) != cfg.local_config.login_qq:
    raise SystemExit("桥接登录账号不符合专用小号配置")
samples = []
for source in cfg.local_config.sources:
    sid, qq = source.id, source.qzone_id
    scans = []
    for scan in range(2):
        pages, cursor = [], None
        for number in range(2):
            r = b.page(qq, cursor)
            posts = [normalize(p, qq) for p in r["msglist"]]
            pages.append(posts)
            print(dumps({"source": sid, "scan": scan + 1, "page": number + 1, "count": len(posts),
                         "image_count": sum(len(p.images) for p in posts), "has_more": r["has_more"],
                         "valid_times": all(p.published_at is not None for p in posts)}), flush=True)
            if not r["has_more"] or not r.get("next_cursor"):
                break
            cursor = r["next_cursor"]
        scans.append(pages)
    signatures = [{p.external_id: content_digest(p.model_dump(mode="json"))
                   for pg in scan for p in pg} for scan in scans]
    first = {p.external_id for p in scans[0][0]}
    second = {p.external_id for p in scans[0][1]} if len(scans[0]) > 1 else set()
    result = {"id": sid, "page_counts": [len(pg) for pg in scans[0]], "second_page_new_ids": len(second-first),
              "two_scans_same_ids": set(signatures[0]) == set(signatures[1]),
              "two_scans_identical": signatures[0] == signatures[1],
              "all_times_parsed": all(p.published_at is not None for pg in scans[0] for p in pg),
              "pagination": "offset; overlap+rescan required", "production_accepted": False}
    report["sources"].append(result)
    all_posts = [p for pg in scans[0] for p in pg]
    if sid == "wall":
        samples.append((sid, [max((p for p in all_posts if p.images), key=lambda p: p.published_at)]))
    else:
        selected = [all_posts[0]]
        card = next((p for p in all_posts if "姓名为" in p.text), None)
        if card and card.external_id != selected[0].external_id:
            selected.append(card)
        samples.append((sid, selected))
path = cfg.data_dir / "access-verification.json"
path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
path.chmod(0o600)
if args.import_samples:
    with httpx.Client(base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + cfg.admin_token},
                      timeout=30, trust_env=False) as client:
        for sid, posts in samples:
            r = client.post("/api/import", json={"source_id": sid, "posts": [p.model_dump(mode="json") for p in posts]})
            r.raise_for_status()
            d = r.json()
            print(dumps({"sample_import": sid, "new": d["new"], "updated": d["updated"], "unchanged": d["unchanged"]}), flush=True)
print("实际账号探测完成；未推进采集进度。报告保存在 data/access-verification.json。")
