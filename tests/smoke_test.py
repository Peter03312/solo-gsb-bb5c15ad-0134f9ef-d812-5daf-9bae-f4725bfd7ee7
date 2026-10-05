"""端到端冒烟测试：覆盖登记、批量上报、幂等、冲突、缺号、
更正留痕、确认丢失、封存不可变、迟到上报与各类错误。

运行：DB_PATH=/tmp/test_agg.db python -m pytest -q
或直接： python tests/smoke_test.py
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

passed = []


def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)


with TestClient(app) as c:
    # 0. 健康检查
    check("health", c.get("/health").json()["status"] == "ok")

    # 1. 创建演练 + 登记两台终端
    r = c.post("/api/drills", json={"name": "2026 应急演练 A"})
    check("create_drill", r.status_code == 201, r.text)
    drill = r.json()["id"]

    r = c.post(f"/api/drills/{drill}/terminals", json={"code": "T-01", "name": "指挥车"})
    check("register_t01", r.status_code == 201, r.text)
    r = c.post(f"/api/drills/{drill}/terminals", json={"code": "T-02"})
    check("register_t02", r.status_code == 201, r.text)
    r = c.post(f"/api/drills/{drill}/terminals", json={"code": "T-01"})
    check("dup_terminal_409", r.status_code == 409 and
          r.json()["error"]["code"] == "terminal_duplicate", r.text)

    # 非法终端上报
    r = c.post(f"/api/drills/{drill}/terminals/GHOST/records/batch",
               json={"records": [{"seq": 1, "occurred_at": "2026-10-05T01:00:00Z",
                                  "record_type": "log", "content": "x"}]})
    check("illegal_terminal_404", r.status_code == 404 and
          r.json()["error"]["code"] == "terminal_not_registered", r.text)

    # 不存在的演练
    r = c.post("/api/drills/nope/terminals", json={"code": "X"})
    check("drill_not_found", r.status_code == 404 and
          r.json()["error"]["code"] == "drill_not_found")

    # 2. 批量上报 T-01: 1,2,4（缺 3）
    batch = {"records": [
        {"seq": 2, "occurred_at": "2026-10-05T02:00:00Z", "record_type": "event",
         "content": "二号事件"},
        {"seq": 4, "occurred_at": "2026-10-05T01:30:00Z", "record_type": "alarm",
         "content": "四号告警"},
        {"seq": 1, "occurred_at": "2026-10-05T01:00:00Z", "record_type": "log",
         "content": "一号日志"},
        {"seq": 0, "occurred_at": "2026-10-05T01:00:00Z", "record_type": "log",
         "content": "非法序号"},
    ]}
    r = c.post(f"/api/drills/{drill}/terminals/T-01/records/batch", json=batch)
    body = r.json()
    check("batch_200", r.status_code == 200, r.text)
    statuses = [x["status"] for x in body["results"]]
    check("batch_statuses", statuses == ["accepted", "accepted", "accepted", "rejected"],
          statuses)
    check("invalid_seq", body["results"][3]["error_code"] == "invalid_seq")
    check("batch_summary",
          body["summary"] == {"total": 4, "accepted": 3, "duplicate": 0,
                              "conflict": 0, "rejected": 1}, body["summary"])

    # 3. 幂等：完全相同再报一次
    r = c.post(f"/api/drills/{drill}/terminals/T-01/records/batch", json={
        "records": [{"seq": 1, "occurred_at": "2026-10-05T01:00:00Z",
                     "record_type": "log", "content": "一号日志"}]})
    b = r.json()
    check("idempotent", b["results"][0]["status"] == "duplicate"
          and b["results"][0]["idempotent"] is True, b)

    # 4. 冲突：seq=1 内容不同
    r = c.post(f"/api/drills/{drill}/terminals/T-01/records/batch", json={
        "records": [{"seq": 1, "occurred_at": "2026-10-05T01:00:00Z",
                     "record_type": "log", "content": "被篡改的内容"}]})
    b = r.json()
    check("conflict_status", b["results"][0]["status"] == "conflict", b)
    check("conflict_evidence", b["results"][0]["existing"]["content"] == "一号日志"
          and b["results"][0]["incoming"]["content"] == "被篡改的内容")
    conflict_id = b["results"][0]["conflict_id"]

    # 原内容未被覆盖
    r = c.get(f"/api/drills/{drill}/terminals/T-01/records/1")
    check("conflict_keeps_original",
          r.json()["content"] == "一号日志" and r.json()["revision_no"] == 0, r.text)

    r = c.get(f"/api/drills/{drill}/conflicts")
    check("conflicts_listed",
          len(r.json()["conflicts"]) == 1
          and r.json()["conflicts"][0]["conflict_id"] == conflict_id, r.text)

    # 批次内重复序号
    r = c.post(f"/api/drills/{drill}/terminals/T-02/records/batch",
               json={"records": [
                   {"seq": 1, "occurred_at": "2026-10-05T03:00:00Z",
                    "record_type": "log", "content": "a"},
                   {"seq": 1, "occurred_at": "2026-10-05T03:01:00Z",
                    "record_type": "log", "content": "b"}]})
    check("dup_in_batch", [x["status"] for x in r.json()["results"]]
          == ["accepted", "rejected"], r.text)

    # 5. 时间线排序 + 缺号
    r = c.get(f"/api/drills/{drill}/timeline")
    tl = r.json()
    check("tl_order", [x["seq"] for x in tl["records"]] == [1, 4, 2, 1],
          [(x["terminal_code"], x["seq"], x["occurred_at"]) for x in tl["records"]])
    t01 = next(t for t in tl["terminals"] if t["terminal_code"] == "T-01")
    check("gap_listed", t01["missing"] == [{"seq": 3, "status": "open"}],
          t01["missing"])
    check("gaps_not_closed", tl["summary"]["all_gaps_closed"] is False
          and tl["summary"]["open_gap_count"] == 1)

    # 6. 未闭合缺号不能封存
    r = c.post(f"/api/drills/{drill}/seals")
    check("seal_blocked_409", r.status_code == 409 and
          r.json()["error"]["code"] == "open_gaps_remain"
          and r.json()["error"]["open_gaps"] == [{"terminal_code": "T-01", "seq": 3}],
          r.text)

    # 7. 更正（附理由）——缺号未补齐也可更正
    r = c.patch(f"/api/drills/{drill}/terminals/T-01/records/2",
                json={"content": "二号事件（修订）", "reason": "现场补充细节"})
    check("correct_ok", r.status_code == 200 and
          r.json()["record"]["revision_no"] == 1, r.text)

    # 无变化更正报错
    r = c.patch(f"/api/drills/{drill}/terminals/T-01/records/2",
                json={"content": "二号事件（修订）", "reason": "再次提交"})
    check("correct_no_change", r.status_code == 400 and
          r.json()["error"]["code"] == "no_change", r.text)

    # 跨演练更正：另一演练查不到该终端 -> 404
    r = c.post("/api/drills", json={"name": "演练 B"})
    drill_b = r.json()["id"]
    r = c.patch(f"/api/drills/{drill_b}/terminals/T-01/records/2",
                json={"content": "跨演练", "reason": "x"})
    check("cross_drill_correct", r.status_code == 404 and
          r.json()["error"]["code"] == "terminal_not_registered", r.text)

    # 修订历史：原文 rev0 + 修订 rev1
    r = c.get(f"/api/drills/{drill}/terminals/T-01/records/2/revisions")
    revs = r.json()["revisions"]
    check("revisions", [x["revision_no"] for x in revs] == [0, 1]
          and revs[0]["content"] == "二号事件"
          and revs[1]["content"] == "二号事件（修订）"
          and revs[1]["reason"] == "现场补充细节"
          and revs[0]["is_original"] is True, r.text)

    # 8. 确认 T-01 seq3 丢失（闭合缺号），重复确认幂等
    r = c.post(f"/api/drills/{drill}/lost", json={"items": [
        {"terminal_code": "T-01", "seq": 3, "reason": "设备损毁未传出"}]})
    check("lost_confirmed", r.json()["results"][0]["status"] == "confirmed", r.text)
    r = c.post(f"/api/drills/{drill}/lost", json={"items": [
        {"terminal_code": "T-01", "seq": 3}]})
    check("lost_idempotent", r.json()["results"][0]["status"] == "duplicate"
          and r.json()["results"][0]["idempotent"] is True, r.text)
    # 已确认丢失的序号不能再上报
    r = c.post(f"/api/drills/{drill}/terminals/T-01/records/batch", json={
        "records": [{"seq": 3, "occurred_at": "2026-10-05T02:30:00Z",
                     "record_type": "log", "content": "迟到的三号"}]})
    check("lost_rejects_upload",
          r.json()["results"][0]["status"] == "rejected"
          and r.json()["results"][0]["error_code"] == "seq_confirmed_lost", r.text)
    # 有记录的序号不能确认丢失
    r = c.post(f"/api/drills/{drill}/lost", json={"items": [
        {"terminal_code": "T-01", "seq": 1}]})
    check("lost_has_record", r.json()["results"][0]["error_code"] == "seq_has_record",
          r.text)

    # 缺号在时间线中显式标为 confirmed_lost
    tl = c.get(f"/api/drills/{drill}/timeline").json()
    t01 = next(t for t in tl["terminals"] if t["terminal_code"] == "T-01")
    check("gap_closed_lost", t01["missing"][0]["status"] == "confirmed_lost"
          and tl["summary"]["all_gaps_closed"] is True, tl)

    # 9. 封存 v1
    r = c.post(f"/api/drills/{drill}/seals")
    check("seal_v1", r.status_code == 201 and r.json()["version"] == 1
          and r.json()["record_count"] == 4, r.text)
    v1_snapshot = r.json()["snapshot"]

    # 10. 封存后迟到上报 + 更正：当前时间线接纳，但封存版本不变
    r = c.post(f"/api/drills/{drill}/terminals/T-02/records/batch", json={
        "records": [{"seq": 2, "occurred_at": "2026-10-05T04:00:00Z",
                     "record_type": "note", "content": "迟到上报"}]})
    check("late_accepted", r.json()["results"][0]["status"] == "accepted", r.text)
    r = c.patch(f"/api/drills/{drill}/terminals/T-01/records/1",
                json={"content": "一号日志（封存后更正）", "reason": "笔误"})
    check("post_seal_correct", r.status_code == 200, r.text)

    r = c.get(f"/api/drills/{drill}/seals/1")
    snap = r.json()["snapshot"]
    check("seal_immutable", snap == v1_snapshot
          and snap["summary"]["record_count"] == 4
          and snap["records"][0]["content"] == "一号日志", "封存快照被改写")

    # 当前时间线已含变更
    tl = c.get(f"/api/drills/{drill}/timeline").json()
    check("current_timeline_evolves", tl["summary"]["record_count"] == 5
          and next(x for x in tl["records"]
                   if x["terminal_code"] == "T-01" and x["seq"] == 1)["content"]
          == "一号日志（封存后更正）", "当前时间线未接纳变更")

    # 再次封存 -> v2，v1 仍在
    r = c.post(f"/api/drills/{drill}/seals")
    check("seal_v2", r.status_code == 201 and r.json()["version"] == 2
          and r.json()["record_count"] == 5, r.text)
    r = c.get(f"/api/drills/{drill}/seals")
    check("seal_list", [s["version"] for s in r.json()["seals"]] == [1, 2], r.text)

    # 11. 新演练制造新缺号 -> 封存被拒（新缺号显式列出）
    r = c.post("/api/drills", json={"name": "演练 C"})
    dc = r.json()["id"]
    c.post(f"/api/drills/{dc}/terminals", json={"code": "T9"})
    c.post(f"/api/drills/{dc}/terminals/T9/records/batch",
           json={"records": [
               {"seq": 1, "occurred_at": "2026-10-05T00:00:00Z",
                "record_type": "a", "content": "x"},
               {"seq": 3, "occurred_at": "2026-10-05T00:01:00Z",
                "record_type": "a", "content": "y"}]})
    r = c.post(f"/api/drills/{dc}/seals")
    check("new_drill_gap_block", r.status_code == 409 and
          r.json()["error"]["open_gaps"] == [{"terminal_code": "T9", "seq": 2}], r.text)

    # 不存在的封存版本
    r = c.get(f"/api/drills/{drill}/seals/99")
    check("seal_not_found", r.status_code == 404 and
          r.json()["error"]["code"] == "seal_not_found")

    # 12. 非法发生时间在批次内逐条拒绝，不影响同批其他条目
    r = c.post("/api/drills", json={"name": "演练 D"})
    dd = r.json()["id"]
    c.post(f"/api/drills/{dd}/terminals", json={"code": "TX"})
    r = c.post(f"/api/drills/{dd}/terminals/TX/records/batch", json={
        "records": [
            {"seq": 1, "occurred_at": "not-a-time", "record_type": "log",
             "content": "坏时间"},
            {"seq": 2, "occurred_at": "2026-10-05T03:00:00Z",
             "record_type": "log", "content": "正常条目"},
        ]})
    rb = r.json()
    check("invalid_time_rejected",
          [x["status"] for x in rb["results"]] == ["rejected", "accepted"]
          and rb["results"][0]["error_code"] == "invalid_occurred_at", rb)
    # 空批次被模型层拒绝
    check("empty_batch_422", c.post(
        f"/api/drills/{dd}/terminals/TX/records/batch",
        json={"records": []}).status_code == 422)

    # 13. 预确认超出已见最大序号的丢失号：不制造幻影缺号，单独展示
    r = c.post("/api/drills", json={"name": "演练 E"})
    de = r.json()["id"]
    c.post(f"/api/drills/{de}/terminals", json={"code": "TE"})
    c.post(f"/api/drills/{de}/terminals/TE/records/batch", json={"records": [
        {"seq": 1, "occurred_at": "2026-10-05T00:00:00Z",
         "record_type": "log", "content": "a"}]})
    r = c.post(f"/api/drills/{de}/lost", json={"items": [
        {"terminal_code": "TE", "seq": 3, "reason": "已知丢失，当前尚未报到2号"}]})
    check("preconfirm_lost", r.json()["results"][0]["status"] == "confirmed")
    te = next(t for t in c.get(f"/api/drills/{de}/timeline").json()["terminals"]
              if t["terminal_code"] == "TE")
    check("no_phantom_gap",
          te["missing"] == [] and te["max_seq"] == 1
          and [m["seq"] for m in te["confirmed_lost_beyond_observed"]] == [3], te)
    # 此时没有 open 缺号（max_seq=1 连续），允许封存；快照中保留预确认信息
    r = c.post(f"/api/drills/{de}/seals")
    check("seal_with_preconfirm", r.status_code == 201, r.text)
    check("preconfirm_rejects_upload",
          c.post(f"/api/drills/{de}/terminals/TE/records/batch", json={"records": [
              {"seq": 3, "occurred_at": "2026-10-05T00:10:00Z",
               "record_type": "log", "content": "迟到的3"}]}).json()["results"][0]
          ["error_code"] == "seq_confirmed_lost")

print(f"\n全部 {len(passed)} 项检查通过：")
for n in passed:
    print("  ✓", n)
os.unlink(_tmp.name)
