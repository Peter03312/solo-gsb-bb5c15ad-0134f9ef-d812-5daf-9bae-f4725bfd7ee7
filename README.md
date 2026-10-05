# 应急演练离线终端记录汇聚服务

面向应急演练场景的离线终端记录汇聚服务：**Python 3.12 + FastAPI + SQLite**。

- 每场演练先登记终端，终端以**从 1 递增的本地序号批量上报**发生时间、类型和内容；
- 同一终端同一序号**内容一致的重复提交幂等**，**内容不一致报冲突并留存冲突证据**（原文不被覆盖）；
- 当前时间线按 **发生时间 → 终端标识 → 序号** 稳定排序，**缺号显式列出**；
- 记录可**附理由更正**，**原文与历次修订均可查询**；
- 只有**所有终端缺号已补齐或逐号确认丢失**才能封存；**封存版本不可变**，
  迟到上报与更正仍进入当前时间线，并可形成新的封存版本；
- 非法终端、无效序号、跨演练更正、未闭合缺号的封存均返回明确错误。

## 一、一键启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务地址：`http://localhost:8080`
- API 前缀：`http://localhost:8080/api`
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`
- 数据库：容器内 `/data/aggregator.db`，通过命名卷 `aggregator-data` 持久化；
  容器启动时由应用自动幂等执行建表脚本（`app/schema.sql`），无需手工初始化。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库卷
```

仅用 Docker 启动（不使用 Compose）：

```bash
docker build -t drill-record-aggregator:1.0.0 .
docker run -d -p 8080:8080 -v aggregator-data:/data drill-record-aggregator:1.0.0
```

## 二、本地直接运行（开发）

```bash
pip install -r requirements.txt
DB_PATH=./aggregator.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

运行端到端冒烟测试（45 项断言，覆盖全部业务规则）：

```bash
DB_PATH=/tmp/test.db PYTHONPATH=. python tests/smoke_test.py
```

## 三、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/aggregator.db` | SQLite 数据库文件路径，目录不存在会自动创建 |
| `API_PREFIX` | `/api` | 所有业务接口的路径前缀 |

监听地址与端口由 uvicorn 参数控制（Dockerfile/Compose 固定为 `0.0.0.0:8080`，
本地运行可自行调整 `--host` / `--port`）。

## 四、HTTP API 一览

除健康检查外均以 `/api` 为前缀。错误响应统一形如：

```json
{"error": {"code": "open_gaps_remain", "message": "…", "open_gaps": [ … ]}}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/drills` | 创建演练 |
| GET | `/api/drills` | 演练列表 |
| GET | `/api/drills/{drill_id}` | 演练详情（含时间线汇总） |
| POST | `/api/drills/{drill_id}/terminals` | 登记终端（演练内 `code` 唯一） |
| GET | `/api/drills/{drill_id}/terminals` | 终端列表 |
| POST | `/api/drills/{drill_id}/terminals/{terminal_code}/records/batch` | **批量上报**，逐条返回状态 |
| GET | `/api/drills/{drill_id}/timeline` | **当前时间线**（稳定排序 + 缺号） |
| GET | `/api/drills/{drill_id}/terminals/{terminal_code}/records/{seq}` | 查单条当前记录 |
| PATCH | `/api/drills/{drill_id}/terminals/{terminal_code}/records/{seq}` | **附理由更正** |
| GET | `/api/drills/{drill_id}/terminals/{terminal_code}/records/{seq}/revisions` | **原文 + 历次修订** |
| GET | `/api/drills/{drill_id}/conflicts` | 冲突证据列表 |
| POST | `/api/drills/{drill_id}/lost` | 逐号确认丢失（闭合缺号） |
| POST | `/api/drills/{drill_id}/seals` | **封存**（存在未闭合缺号时 409） |
| GET | `/api/drills/{drill_id}/seals` | 封存版本列表 |
| GET | `/api/drills/{drill_id}/seals/{version}` | 取指定封存版本（不可变快照） |
| GET | `/health` | 健康检查 |

### 批量上报的逐条状态

HTTP 状态码恒为 `200`，通过 `results[].status` 区分：

| status | 含义 |
| --- | --- |
| `accepted` | 新接收 |
| `duplicate` | 同序号且内容完全一致，幂等返回，不产生写入 |
| `conflict` | 同序号内容不一致，**拒绝覆盖并留存证据**，响应附带 `conflict_id`、`existing`、`incoming` |
| `rejected` | 条目级拒绝，`error_code` 标明原因：`invalid_seq`（序号 < 1）、`duplicate_in_batch`、`seq_confirmed_lost` |

终端未登记 / 演练不存在等请求级错误直接返回 `404`，不产生任何写入。

### 缺号与封存规则

- 时间线中每个终端给出 `missing`：`{"seq": n, "status": "open"}` 为未闭合缺号，
  `{"seq": n, "status": "confirmed_lost", ...}` 为已逐号确认丢失；
- 只要任一终端存在 `open` 缺号，封存返回 **409 `open_gaps_remain`**，并列出全部缺号；
- 封存是当时时间线的**完整 JSON 快照**（`seals.snapshot`），写入后再无更新路径；
  迟到上报、更正只改变当前时间线；缺号再次全部闭合后可封存为下一版本（版本号递增）。

## 五、调用示例

完整可执行脚本：[`examples/curl-examples.sh`](examples/curl-examples.sh)

```bash
docker compose up --build -d
bash examples/curl-examples.sh
```

下面是关键片段。

**1）创建演练、登记终端**

```bash
curl -s -X POST http://localhost:8080/api/drills \
  -H 'Content-Type: application/json' \
  -d '{"name":"2026 城市防汛应急演练"}'
# -> {"id":"<DRILL_ID>", ...}

curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/terminals \
  -H 'Content-Type: application/json' \
  -d '{"code":"T-001","name":"现场指挥车"}'
```

**2）批量上报（序号 1/2/4，缺 3）**

```bash
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/terminals/T-001/records/batch \
  -H 'Content-Type: application/json' \
  -d '{"records":[
    {"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"终端开机自检"},
    {"seq":2,"occurred_at":"2026-10-05T08:15:00Z","record_type":"event","content":"发现积水点 A"},
    {"seq":4,"occurred_at":"2026-10-05T09:05:00Z","record_type":"alarm","content":"泵站液位告警"}
  ]}'
```

返回（逐条接收状态 + 汇总）：

```json
{
  "results": [
    {"index":0,"seq":1,"status":"accepted","record_id":"…","revision_no":0},
    {"index":1,"seq":2,"status":"accepted","record_id":"…","revision_no":0},
    {"index":2,"seq":4,"status":"accepted","record_id":"…","revision_no":0}
  ],
  "summary": {"total":3,"accepted":3,"duplicate":0,"conflict":0,"rejected":0}
}
```

**3）幂等与冲突**

```bash
# 内容完全一致 -> status=duplicate（幂等，无写入）
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/terminals/T-001/records/batch \
  -H 'Content-Type: application/json' \
  -d '{"records":[{"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"终端开机自检"}]}'

# 内容不同 -> status=conflict，原文保留，证据落库
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/terminals/T-001/records/batch \
  -H 'Content-Type: application/json' \
  -d '{"records":[{"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"被篡改的开机记录"}]}'
# -> {"results":[{"seq":1,"status":"conflict","conflict_id":"…",
#     "existing":{"content":"终端开机自检",…},"incoming":{"content":"被篡改的开机记录",…}}]}

curl -s http://localhost:8080/api/drills/<DRILL_ID>/conflicts   # 查询全部冲突证据
```

**4）当前时间线与缺号**

```bash
curl -s http://localhost:8080/api/drills/<DRILL_ID>/timeline
```

```json
{
  "records": [ /* 按 occurred_at, terminal_code, seq 稳定排序 */ ],
  "terminals": [{
    "terminal_code": "T-001", "max_seq": 4, "record_count": 3,
    "missing": [{"seq": 3, "status": "open"}],
    "confirmed_lost_beyond_observed": []
  }],
  "summary": {"terminal_count":1,"record_count":3,"open_gap_count":1,
              "confirmed_lost_count":0,"all_gaps_closed":false}
}
```

`confirmed_lost_beyond_observed` 列出“超出已见最大序号”的预确认丢失号——
缺号只相对于已见最大序号判定，不会对尚未观测到的尾部序号制造幻影缺号；
预确认号同样拒绝后续上报。

**5）更正与修订留痕**

```bash
curl -s -X PATCH http://localhost:8080/api/drills/<DRILL_ID>/terminals/T-001/records/2 \
  -H 'Content-Type: application/json' \
  -d '{"content":"发现积水点 A，水深 30cm","reason":"现场复测补充水深数据"}'

curl -s http://localhost:8080/api/drills/<DRILL_ID>/terminals/T-001/records/2/revisions
# revision 0 = 原始上报；其后每次更正追加一条，含 reason 与 created_at
```

跨演练更正（终端未登记于目标演练）返回 `404 terminal_not_registered`。

**6）确认丢失、封存、封存后变更**

```bash
# 未闭合缺号封存 -> 409 open_gaps_remain（响应中列出 open_gaps）
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/seals

# 逐号确认丢失
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/lost \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"terminal_code":"T-001","seq":3,"reason":"终端断电未及传出"}]}'

# 再次封存 -> 201，version=1（快照不可变）
curl -s -X POST http://localhost:8080/api/drills/<DRILL_ID>/seals

# 封存后迟到上报 / 更正照常进入当前时间线；之后可封存为 version=2
curl -s http://localhost:8080/api/drills/<DRILL_ID>/seals/1   # v1 内容永不改变
curl -s http://localhost:8080/api/drills/<DRILL_ID>/timeline  # 当前时间线持续演进
```

## 六、错误码速查

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `drill_not_found` | 演练不存在 |
| 404 | `terminal_not_registered` | 非法终端上报 / 更正（含跨演练更正） |
| 409 | `terminal_duplicate` | 终端标识重复登记 |
| 400 | `invalid_seq` | 序号 `< 1` |
| 400 | `no_change` | 更正内容与现状完全相同 |
| 404 | `record_not_found` | 更正/查询的序号尚无记录 |
| 409 | `open_gaps_remain` | 仍有未闭合缺号时封存（附 `open_gaps` 明细） |
| 404 | `seal_not_found` | 封存版本不存在 |

条目级错误（批量接口内）：`invalid_seq`、`duplicate_in_batch`、`seq_confirmed_lost`、
`seq_has_record`（确认丢失时该号已有记录）、`terminal_not_registered`。

## 七、并发与存储说明

- SQLite 开启 WAL；所有写事务使用 `BEGIN IMMEDIATE` 串行化，
  同终端同序号的并发提交只会有一条 `accepted`，其余按内容判定为 `duplicate`/`conflict`，
  并发封存产生严格递增且互不相同的版本号（已用 40 路并发压测验证）；
- 时间戳统一 UTC ISO-8601；发生时间支持 `Z` 后缀，无时区按 UTC 解析。

## 八、项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 应用：模型、接口、时间线/封存逻辑
│   └── schema.sql       # SQLite 建表脚本（启动时幂等执行）
├── tests/
│   └── smoke_test.py    # 端到端冒烟测试（45 项断言）
├── examples/
│   └── curl-examples.sh # 完整 curl 调用示例
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```
