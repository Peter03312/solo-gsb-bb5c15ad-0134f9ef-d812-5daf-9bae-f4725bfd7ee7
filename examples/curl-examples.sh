#!/usr/bin/env bash
# 端到端调用示例（curl）。
# 用法：
#   BASE=http://localhost:8080 API=/api bash examples/curl-examples.sh
# 依赖：curl、python3（仅用于美化输出）
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="${API:-/api}"
pp() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo "== 1. 创建演练 =="
DRILL=$(curl -s -X POST "$BASE$API/drills" \
  -H 'Content-Type: application/json' \
  -d '{"name":"2026 城市防汛应急演练"}')
echo "$DRILL" | pp
DRILL_ID=$(echo "$DRILL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 2. 登记离线终端（先登记，后上报）=="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals" \
  -H 'Content-Type: application/json' \
  -d '{"code":"T-001","name":"现场指挥车"}' | pp
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals" \
  -H 'Content-Type: application/json' \
  -d '{"code":"T-002","name":"排水班组终端"}' | pp

echo "== 3. T-001 批量上报本地序号 1/2/4（故意缺 3）=="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/batch" \
  -H 'Content-Type: application/json' \
  -d '{"records":[
    {"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"终端开机自检"},
    {"seq":2,"occurred_at":"2026-10-05T08:15:00Z","record_type":"event","content":"发现积水点 A"},
    {"seq":4,"occurred_at":"2026-10-05T09:05:00Z","record_type":"alarm","content":"泵站液位告警"}
  ]}' | pp

echo "== 4. 重复上报 seq=1（内容一致 -> 幂等 duplicate）=="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/batch" \
  -H 'Content-Type: application/json' \
  -d '{"records":[
    {"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"终端开机自检"}
  ]}' | pp

echo "== 5. 冲突上报 seq=1（内容不同 -> conflict，证据留存，原文不变）=="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/batch" \
  -H 'Content-Type: application/json' \
  -d '{"records":[
    {"seq":1,"occurred_at":"2026-10-05T08:00:00Z","record_type":"log","content":"被篡改的开机记录"}
  ]}' | pp

echo "== 6. 查询冲突证据列表 =="
curl -s "$BASE$API/drills/$DRILL_ID/conflicts" | pp

echo "== 7. 当前时间线（按 发生时间/终端标识/序号 排序，显式列出缺号 3）=="
curl -s "$BASE$API/drills/$DRILL_ID/timeline" | pp

echo "== 8. 缺号未闭合时封存 -> 409 明确报错 =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE$API/drills/$DRILL_ID/seals"

echo "== 9. 附理由更正 seq=2 =="
curl -s -X PATCH "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/2" \
  -H 'Content-Type: application/json' \
  -d '{"content":"发现积水点 A，水深 30cm","reason":"现场复测补充水深数据"}' | pp

echo "== 10. 查询 seq=2 原文与历次修订（revision 0 为原文）=="
curl -s "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/2/revisions" | pp

echo "== 11. 逐号确认 seq=3 丢失，闭合缺号 =="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/lost" \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"terminal_code":"T-001","seq":3,"reason":"终端断电，序号3未及传出"}]}' | pp

echo "== 12. 封存 v1（成功）=="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/seals" | pp

echo "== 13. 封存后迟到上报 + 更正：当前时间线接纳 =="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/terminals/T-002/records/batch" \
  -H 'Content-Type: application/json' \
  -d '{"records":[
    {"seq":1,"occurred_at":"2026-10-05T07:40:00Z","record_type":"event","content":"班组出动（迟到上报）"}
  ]}' | pp
curl -s -X PATCH "$BASE$API/drills/$DRILL_ID/terminals/T-001/records/4" \
  -H 'Content-Type: application/json' \
  -d '{"record_type":"alarm","content":"泵站液位告警（已复核）","reason":"告警复核后补充结论"}' | pp

echo "== 14. 封存版本 v1 内容不变；当前时间线已含变更 =="
curl -s "$BASE$API/drills/$DRILL_ID/seals/1" | pp
curl -s "$BASE$API/drills/$DRILL_ID/timeline" | pp

echo "== 15. 变更闭合后再次封存 -> v2 =="
curl -s -X POST "$BASE$API/drills/$DRILL_ID/seals" | pp

echo
echo "演练 ID: $DRILL_ID （可用于后续查询）"
