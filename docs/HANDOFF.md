# 下游交接协议 1.0（联调版本）

基址：`http://127.0.0.1:8766`。请求使用 `Authorization: Bearer <DOWNSTREAM_TOKEN>`。管理令牌不能用于下游拉取，下游令牌不能修改管理数据。跨设备接入前需另行配置 TLS 和受控访问；当前仅绑定本机。

## 拉取与确认

1. `POST /api/downstream/pull?limit=50`：返回当前到期的未确认消息，同时产生一次投递租期。最多 100 条。**拉取不等于接收确认。**
2. 将载荷写入下游事务数据库，以 `idempotency_key` 唯一约束去重，并按 `event_id` 保存最大 `event_version`。
3. 持久化提交成功后，`POST /api/downstream/messages/{message_id}/ack`。重复确认安全。
4. 确认失败或丢失会再次投递同一消息与幂等键。投递间隔从 30 秒指数退避，上限一小时；8 次未确认进入手工重试列表。

```json
{
  "messages": [
    {
      "message_id": 1,
      "idempotency_key": "event-id:1:upsert",
      "payload": {
        "schema_version": "1.0",
        "event_id": "event-id",
        "event_version": 1,
        "event_action": "upsert",
        "event_type": "found_item",
        "event_status": "ready",
        "items": ["校园卡"],
        "event_time": null,
        "locations": {"found": "快递站附近", "stored": "置物架"},
        "claim_instructions": null,
        "persons": [{"role": "card_holder", "name": "虚构同学", "identifier": null,
          "school": null, "campus": null,
          "evidence": [{"kind": "text", "quote": "姓名为虚构同学", "image_index": null, "location": null}]}],
        "identity_level": "I1",
        "identity_evidence": [{"kind": "text", "quote": "姓名为虚构同学", "image_index": null, "location": null}],
        "identity_confirmed": false,
        "match_status": "pending",
        "source_refs": [{"source_id": "rights", "post_id": "post-id", "external_id": "qq-post-id",
          "post_version": 1, "original_url": "https://user.qzone.qq.com/example", "published_at": null,
          "collected_at": "2026-10-24T20:00:00+08:00", "window": null, "fingerprint_risk": false,
          "evidence_url": "/api/evidence/posts/post-id"}],
        "evidence_refs": [{"kind": "post", "ref": "/api/evidence/posts/post-id"}],
        "missing_fields": [],
        "conflicts": [],
        "review_required": true,
        "model_version": "deepseek-flash/qzone-extract-1.1",
        "processed_at": "2026-10-24T20:01:00+08:00",
        "reason": "有清晰姓名与招领依据"
      }
    }
  ]
}
```

`source_refs.window` 非空时包含 `window_start`、`window_end` 和 `coverage`；人工导入的代表样本为 `null`。补采在原始记录中单独标记，不改变原始发布时间。

## 版本、撤回与关闭

`GET /api/downstream/events/{event_id}/latest` 返回当前版本、状态、处理时间和合并目标。下游在审核和发送前再次查询；载荷版本不等于最新版本时停止旧审核。旧版本永远不能覆盖新版本。仅比较接收顺序不安全。

- `upsert`：有用的失主线索，可查询人员并进入审核。I1 姓名可用、I2 标识可用、I3 人工核实。邮箱可以为空。
- `close`：业务已解决或撤回求助，使旧任务过期。
- `retract`：误识别、原文改变待复核、证据失效或不再具备资格，使旧任务过期；不表示物品已归还。

`close / retract` 最小载荷为：`schema_version`、`event_id`、更大的 `event_version`、`event_action`、`reason`、`processed_at`。不要求旧的人物字段仍然有效。

原文编辑在重新识别完成前会撤回旧交接资格。已确认的旧消息仍保留接收记录，新版本必须继续消费；确认只代表持久化，不代表认可或允许发送。已过期消息的确认返回 409，应查询最新版本；普通重复确认返回成功。

## 证据与权限

`GET /api/evidence/posts/{post_id}` 返回当前原文与版本。`GET /api/evidence/media/{media_id}` 返回图片文件。相对路径均以服务基址解析，并使用相同下游令牌鉴权。

下游只可读已经进入交接范围的证据；不能遍历其他无关帖子。证据到期/旧媒体版本返回 410，证据未授权返回 403。证据失效需人工复核，不能推断为已经归还。QQ 会话、Cookie、API Key 不进入载荷。

自动默认保留原文/媒体 30 天，关闭后的结构化信息与操作记录 90 天。正式接入双方须确认审核周期、下游独立数据保留和删除联动方式。不要将联调导出公开上传 GitHub。

## 下游必须完成的业务

姓名查询可返回多个合理候选；每个候选分别审核、分别发送，使用 `(event_id, recipient_id)` 或同等约束防重复。邮件措辞应保留不确定性。发送状态和发送授权由邮件模块维护，本服务没有发件接口或发件凭据。

管理端可用 `GET /api/handoffs/export?after=0&limit=100` 按递增消息序号稳定导出历史。导出不确认接收，可能包含已过期版本；消费仍须查询最新版本。模型输出 JSON Schema 与 OpenAPI 分别见受管理令牌保护的 `/api/schema` 和 `/api/openapi.json`。
