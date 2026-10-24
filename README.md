# QQ 空间失物线索 bot

按北京时间每天 19 轮读取两个已授权的 QQ 空间，识别寻物与招领，保留原文和图片证据，并把可供人员匹配的线索交给下游。**清晰姓名即可交接；交接不等于身份认证或批准发邮件。**

本项目按 [PRD v0.5](QQ空间获取bot_PRD_v0.5.md) 实现。管理页用于观察运行状态、核实与纠错；也提供命令行和 HTTP 接口，不需要一直开着页面。

## 当前能力

- 两个来源独立采集与恢复；固定每轮上限，分页、重读边界、重复扫描验证，覆盖不确定时不推进成功时间。
- SQLite 持久化；原始动态先去重再识别，编辑产生新版本。图片链接临时签名变化不会触发新版本或重复识别。
- DeepSeek `deepseek-flash` 图文抽取、多个事件拆分、失主/发布者/代发者角色与文字/图片证据校验。缺失或冲突正常进入待核实。
- 图片补抓、模型错误独立重试；每日 5 元预算默认值，调用前预留预算，达到上限暂停模型并保留任务。
- 人工修正、关闭、合并及审计；模型重跑保留人工核实内容，作为新建议展示。原文编辑会暂停旧线索的交接资格。
- 持久交接队列，`upsert / close / retract`、版本查询、显式接收确认、幂等键、重投及 JSON 导出。
- 受令牌保护的管理页与证据接口，独立下游令牌；登录信息与模型 Key 只留本机。
- 原文/媒体默认 30 天、关闭事件及操作记录默认 90 天清理；证据过期撤回未处理线索。

**当前是小范围试运行，不代表 PRD 的正式验收已完成。** 已实际验证两个来源的只读访问、分页和少量图文样本，以及本地模拟下游接收。200 条人工标注、精确率/召回率、72 小时稳定运行及正式下游对接尚待完成。具体见 [验收记录](docs/ACCEPTANCE.md)。

## 本机快速启动

要求 Python ≥3.11、Node.js ≥18、Git、Chrome。推荐使用 `uv`。

```bash
uv sync --locked
.venv/bin/python scripts/setup.py
.venv/bin/python scripts/install_bridge.py
data/bridge/node_modules/.bin/tsx scripts/bridge-server.mjs --login
zsh scripts/start_all.sh
```

首次启动前在本机 `.env` 填写 `DEEPSEEK_API_KEY`，在 `data/local-config.json` 填写账号；保存并重启 Python 服务。不要把 Key 发到聊天、提交 Git 或放进前端代码。管理令牌与下游令牌由初始化程序随机生成。`.env.example` 是不含真实凭据的配置说明。

管理页：[http://127.0.0.1:8766](http://127.0.0.1:8766)。使用 `.env` 中的 `ADMIN_TOKEN` 登录，或打开每次启动生成的 `data/local-login.url` 中的短时一次性登录链接。

初始化会创建被 Git 忽略的 `data/local-config.json`；在本机填写 `login_qq` 和两个来源的名称及 QQ 号码。公开示例均为空占位，不携带真实账号。专用访问账号与来源分开。新的数据目录默认关闭采集，需要确认实际权限、首次起点及分页验证后启用。现有本机的验证记录与开关保存在 `data/qzone.sqlite3`，重启不会重置。

服务只监听本机。**仅启动一个 Python worker**；文件锁防止两个实例同时操作同一数据目录。默认端口为 8766。

## 不打开网页也能操作

```bash
.venv/bin/python scripts/bot.py status
.venv/bin/python scripts/bot.py collect all
.venv/bin/python scripts/bot.py tasks
.venv/bin/python scripts/bot.py retry 12
.venv/bin/python scripts/bot.py import samples/import.example.json
.venv/bin/python scripts/bot.py export data/handoffs.json
```

示例文件里的内容是虚构的。导入进入真实处理队列，可能调用模型；人工导入**不推进**来源采集进度。导出**不代表**下游已接收。

## 调度与恢复

默认 `Asia/Shanghai`，每天 `00:00、04:00、07:00、08:00…22:00、23:00`，共 19 轮。时点可以在 `.env` 的 `SCHEDULE_HOURS` 修改。服务启动或电脑恢复后，从最后成功区间补齐；首次起点取最近的计划时点，不默认抓全部历史。

每个来源单独保存成功时间、边界 ID、批次区间和分页记录。同一来源不会并发；新触发合并到待执行终点。采集、图片、模型和交接分别持久化，模型失败不会让动态丢失。

当前获取方式使用已授权会话下的 QQ 空间个人动态页，属于需持续验证的第三方适配。它使用偏移分页，**每轮都要重读和复查**；结果不一致、空响应、时间不可解析、权限拒绝或达到保护上限时，记录未完成或无法确认。被删除或已经不可见的动态无法保证发现。

保持 Mac 联网与唤醒才能准点运行。需要临时防止空闲休眠时，可在前台运行 `caffeinate -i zsh scripts/start_all.sh`；结束命令后恢复正常休眠行为。不要重复启动当前已运行的实例。

## 下游接入

阅读 [交接协议](docs/HANDOFF.md)，使用独立 `DOWNSTREAM_TOKEN`。本模块不查询人员目录、邮箱，不发送邮件。发送前由下游读取最新版本、检查原文当前状态并完成审核；同名候选可以分别审核和通知。

本地联调示例：

```bash
.venv/bin/python scripts/demo_consumer.py
```

该脚本把接收到的真实载荷写入独立本地数据库并确认，不发送邮件。它会改变消息的接收状态，仅在联调时执行。正式接入应使用自己的持久化、幂等消费、审核与发送记录。

## 开发与验证

```bash
uv sync --locked
.venv/bin/pytest -q
.venv/bin/ruff check app scripts tests
node --check app/static/app.js
.venv/bin/python scripts/verify_access.py
```

只读探测脚本比较两个来源各两页的重复扫描，报告保存在私有 `data/`。加 `--import-samples` 会从可见历史中明确选取少量代表样本导入处理；不会把这些样本伪装成新的计划采集。

测试覆盖原始去重、同秒边界、置顶、翻页不一致、失败恢复、图片缺失、角色与姓名证据、人工保护、原文编辑、关闭/撤回、确认丢失、权限控制及费用预算。模拟测试通过不能代替实际样本的准确率与长期稳定性验证。

## 文件与凭据

| 位置 | 内容 |
| --- | --- |
| `app/` | Python 服务、调度、识别与轻量管理页 |
| `scripts/` | 安装、启动、命令行与联调工具 |
| `tests/` | 独立临时数据库中的自动化测试 |
| `local-config.example.json` | 账号配置模板，所有真实账号均留空 |
| `data/qzone.sqlite3` | 私有业务数据，不提交 Git |
| `data/media/` | 私有图片证据，不提交 Git |
| `data/bridge-session/` | QQ 会话，不提交 Git |
| `data/local-config.json` | 本机账号和来源，不提交 Git |
| `.env` | 本机服务密钥，不提交 Git |

桥接源码固定到上游提交，依赖锁文件纳入项目以便复现。安装过程禁用 npm 生命周期脚本，保留 Chrome 沙箱，关闭上游原始响应日志；只读门面不启动好友流轮询、不暴露发说说/私聊等动作。网络环境、重新登录、保留与备份操作见 [运行说明](docs/OPERATIONS.md)。

公开版本不包含真实账号、密钥、Cookie、数据库、真实动态或运行截图。发布检查和后续提交要求见 [安全与发布说明](SECURITY.md)。
