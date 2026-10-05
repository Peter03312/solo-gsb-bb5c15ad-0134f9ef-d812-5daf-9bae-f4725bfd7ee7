"""应急演练离线终端记录汇聚服务。

技术栈：Python + FastAPI + SQLite（标准库 sqlite3）。

核心业务：
- 演练登记、终端登记；
- 终端以本地序号（从 1 递增）批量上报，逐条返回接收状态；
- 同终端同序号内容一致重复提交幂等，内容不一致报冲突并留存证据；
- 当前时间线按（发生时间, 终端标识, 序号）稳定排序，显式列出缺号；
- 记录可附理由更正，原文与历次修订均可查询；
- 所有终端缺号补齐或逐号确认丢失后方可封存；封存版本不可变，
  迟到上报与更正仍可进入当前时间线并形成后续封存版本。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 配置

DB_PATH = os.getenv("DB_PATH", "/data/aggregator.db")
API_PREFIX = os.getenv("API_PREFIX", "/api")


def now_iso() -> str:
    """UTC ISO-8601，秒级精度 + 显式偏移。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def parse_occurred_at(value: str) -> datetime:
    """解析上报的发生时间，兼容 Z 后缀；无时区按 UTC 处理。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- 数据库

SCHEMA_SQL = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")


def init_db() -> None:
    """幂等初始化：建目录、执行 schema。"""
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def get_conn():
    """每请求一个连接；写操作统一使用 BEGIN IMMEDIATE 串行化。

    FastAPI 的同步依赖解析与端点可能落在线程池的不同线程上，因此
    check_same_thread=False（连接仅在单个请求内串行使用，不跨请求共享）。
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="应急演练离线终端记录汇聚服务",
    version="1.0.0",
    description="离线终端批量上报、幂等/冲突处理、缺号跟踪、更正留痕与封存。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 模型


class DrillIn(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="演练名称")


class TerminalIn(BaseModel):
    code: str = Field(min_length=1, max_length=100, description="终端标识（演练内唯一）")
    name: Optional[str] = Field(default=None, max_length=200, description="终端备注名")


class RecordItem(BaseModel):
    seq: int = Field(description="终端本地序号，从 1 递增")
    occurred_at: str = Field(description="发生时间 ISO-8601（非法时间在批次内逐条拒绝）")
    record_type: str = Field(min_length=1, max_length=100, description="类型")
    content: str = Field(min_length=1, description="内容")


class BatchIn(BaseModel):
    records: List[RecordItem] = Field(min_length=1, description="批量上报条目")


class CorrectIn(BaseModel):
    occurred_at: Optional[str] = Field(default=None, description="更正后的发生时间")
    record_type: Optional[str] = Field(default=None, min_length=1, max_length=100)
    content: Optional[str] = Field(default=None, min_length=1, description="更正后的内容")
    reason: str = Field(min_length=1, max_length=500, description="更正理由（必填）")

    @field_validator("occurred_at")
    @classmethod
    def _check_time(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_occurred_at(v)
        return v


class LostItem(BaseModel):
    terminal_code: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=1)
    reason: Optional[str] = Field(default=None, max_length=500)


class LostBatchIn(BaseModel):
    items: List[LostItem] = Field(min_length=1)


# ---------------------------------------------------------------- 错误

def error(code: str, message: str, http_status: int, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    return JSONResponse(status_code=http_status, content=body)


# ---------------------------------------------------------------- 辅助查询


def get_drill_or_none(conn: sqlite3.Connection, drill_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drills WHERE id = ?", (drill_id,)
    ).fetchone()


def get_terminal_or_none(
    conn: sqlite3.Connection, drill_id: str, terminal_code: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM terminals WHERE drill_id = ? AND code = ?",
        (drill_id, terminal_code),
    ).fetchone()


def get_terminal_by_id(
    conn: sqlite3.Connection, drill_id: str, terminal_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM terminals WHERE id = ? AND drill_id = ?",
        (terminal_id, drill_id),
    ).fetchone()


def record_out(row: sqlite3.Row, terminal_code: Optional[str] = None) -> Dict[str, Any]:
    data = {
        "record_id": row["id"],
        "drill_id": row["drill_id"],
        "terminal_id": row["terminal_id"],
        "seq": row["seq"],
        "occurred_at": row["occurred_at"],
        "record_type": row["record_type"],
        "content": row["content"],
        "revision_no": row["revision_no"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if terminal_code is not None:
        data["terminal_code"] = terminal_code
    return data


def build_timeline(conn: sqlite3.Connection, drill_id: str) -> Dict[str, Any]:
    """构造当前时间线：稳定排序记录 + 每终端缺号清单。"""
    terminals = conn.execute(
        "SELECT * FROM terminals WHERE drill_id = ? ORDER BY code", (drill_id,)
    ).fetchall()
    rec_rows = conn.execute(
        "SELECT r.*, t.code AS terminal_code FROM records r "
        "JOIN terminals t ON t.id = r.terminal_id "
        "WHERE r.drill_id = ?",
        (drill_id,),
    ).fetchall()
    lost_rows = conn.execute(
        "SELECT * FROM lost_sequences WHERE drill_id = ?", (drill_id,)
    ).fetchall()

    # 稳定排序：发生时间 -> 终端标识 -> 序号（Python 侧统一时区后比较）
    def sort_key(r: sqlite3.Row):
        return (parse_occurred_at(r["occurred_at"]), r["terminal_code"], r["seq"])

    ordered = sorted(rec_rows, key=sort_key)
    records = [record_out(r, r["terminal_code"]) for r in ordered]

    lost_map: Dict[str, Dict[int, sqlite3.Row]] = {}
    for lr in lost_rows:
        lost_map.setdefault(lr["terminal_id"], {})[lr["seq"]] = lr

    terminals_out: List[Dict[str, Any]] = []
    open_gap_count = 0
    closed_lost_count = 0
    for t in terminals:
        seqs = sorted(r["seq"] for r in rec_rows if r["terminal_id"] == t["id"])
        present = set(seqs)
        lost = lost_map.get(t["id"], {})
        max_seq = max(seqs, default=0)
        # 缺号只相对于“已见最大序号”判定，避免对未观测到的尾部序号制造幻影缺号
        missing: List[Dict[str, Any]] = []
        for s in range(1, max_seq + 1):
            if s in present:
                continue
            if s in lost:
                missing.append({
                    "seq": s,
                    "status": "confirmed_lost",
                    "reason": lost[s]["reason"],
                    "confirmed_at": lost[s]["created_at"],
                })
                closed_lost_count += 1
            else:
                missing.append({"seq": s, "status": "open"})
                open_gap_count += 1
        # 超出已见最大序号的丢失确认（预确认）单独列出，同样拒绝后续上报
        confirmed_beyond = [{
            "seq": s, "status": "confirmed_lost",
            "reason": lost[s]["reason"], "confirmed_at": lost[s]["created_at"],
        } for s in sorted(lost) if s > max_seq]
        terminals_out.append({
            "terminal_id": t["id"],
            "terminal_code": t["code"],
            "terminal_name": t["name"],
            "max_seq": max_seq,
            "record_count": len(seqs),
            "missing": missing,
            "confirmed_lost_beyond_observed": confirmed_beyond,
        })

    return {
        "drill_id": drill_id,
        "generated_at": now_iso(),
        "records": records,
        "terminals": terminals_out,
        "summary": {
            "terminal_count": len(terminals),
            "record_count": len(records),
            "open_gap_count": open_gap_count,
            "confirmed_lost_count": closed_lost_count
            + sum(len(t["confirmed_lost_beyond_observed"]) for t in terminals_out),
            "all_gaps_closed": open_gap_count == 0,
        },
    }


def latest_seal_version(conn: sqlite3.Connection, drill_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM seals WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    return row["v"] if row and row["v"] is not None else None


# ---------------------------------------------------------------- 基础接口


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post(f"{API_PREFIX}/drills", status_code=status.HTTP_201_CREATED)
def create_drill(payload: DrillIn, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        conn.execute("BEGIN IMMEDIATE")
        did, ts = new_id(), now_iso()
        conn.execute(
            "INSERT INTO drills (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (did, payload.name, ts, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = get_drill_or_none(conn, did)
    return {"id": row["id"], "name": row["name"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.get(f"{API_PREFIX}/drills")
def list_drills(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM terminals t WHERE t.drill_id = d.id) AS terminal_count,"
        " (SELECT MAX(version) FROM seals s WHERE s.drill_id = d.id) AS latest_version"
        " FROM drills d ORDER BY d.created_at"
    ).fetchall()
    return {"drills": [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
         "updated_at": r["updated_at"], "terminal_count": r["terminal_count"],
         "latest_seal_version": r["latest_version"]}
        for r in rows
    ]}


@app.get(f"{API_PREFIX}/drills/{{drill_id}}")
def get_drill(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    drill = get_drill_or_none(conn, drill_id)
    if drill is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    tl = build_timeline(conn, drill_id)
    return {
        "id": drill["id"], "name": drill["name"],
        "created_at": drill["created_at"], "updated_at": drill["updated_at"],
        "latest_seal_version": latest_seal_version(conn, drill_id),
        "timeline_summary": tl["summary"],
    }


@app.post(f"{API_PREFIX}/drills/{{drill_id}}/terminals",
          status_code=status.HTTP_201_CREATED)
def register_terminal(drill_id: str, payload: TerminalIn,
                      conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    if get_terminal_or_none(conn, drill_id, payload.code) is not None:
        return error("terminal_duplicate", f"终端标识 {payload.code} 已登记",
                     status.HTTP_409_CONFLICT, code_value=payload.code)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tid, ts = new_id(), now_iso()
        conn.execute(
            "INSERT INTO terminals (id, drill_id, code, name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, drill_id, payload.code, payload.name, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": tid, "drill_id": drill_id, "code": payload.code,
            "name": payload.name, "created_at": ts}


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/terminals")
def list_terminals(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    rows = conn.execute(
        "SELECT * FROM terminals WHERE drill_id = ? ORDER BY code", (drill_id,)
    ).fetchall()
    return {"terminals": [
        {"id": r["id"], "code": r["code"], "name": r["name"],
         "created_at": r["created_at"]} for r in rows
    ]}


# ---------------------------------------------------------------- 批量上报


@app.post(f"{API_PREFIX}/drills/{{drill_id}}/terminals/{{terminal_code}}/records/batch")
def batch_upload(drill_id: str, terminal_code: str, payload: BatchIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    """批量上报；HTTP 恒为 200，逐条返回 accepted / duplicate / conflict / rejected。"""
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    terminal = get_terminal_or_none(conn, drill_id, terminal_code)
    if terminal is None:
        return error("terminal_not_registered",
                     f"终端 {terminal_code} 未在本演练登记，上报被拒绝",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code)

    results: List[Dict[str, Any]] = []
    accepted = duplicate = conflict = rejected = 0

    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = now_iso()
        seen_in_batch: set = set()

        for idx, item in enumerate(payload.records):
            entry: Dict[str, Any] = {"index": idx, "seq": item.seq}
            try:
                parse_occurred_at(item.occurred_at)
            except (ValueError, TypeError):
                entry.update(status="rejected", error_code="invalid_occurred_at",
                             message="发生时间不是合法的 ISO-8601 时间")
                rejected += 1
                results.append(entry)
                continue
            if item.seq < 1:
                entry.update(status="rejected", error_code="invalid_seq",
                             message="序号必须 >= 1")
                rejected += 1
                results.append(entry)
                continue
            if item.seq in seen_in_batch:
                entry.update(status="rejected", error_code="duplicate_in_batch",
                             message="同一批次内序号重复")
                rejected += 1
                results.append(entry)
                continue

            # 已逐号确认丢失：序号已闭合，迟到上报不能改写该结论
            lost_row = conn.execute(
                "SELECT * FROM lost_sequences WHERE terminal_id = ? AND seq = ?",
                (terminal["id"], item.seq),
            ).fetchone()
            if lost_row is not None:
                entry.update(status="rejected", error_code="seq_confirmed_lost",
                             message="该序号已被确认丢失，不再接收上报")
                rejected += 1
                results.append(entry)
                continue

            existing = conn.execute(
                "SELECT * FROM records WHERE terminal_id = ? AND seq = ?",
                (terminal["id"], item.seq),
            ).fetchone()

            incoming = {"occurred_at": item.occurred_at,
                        "record_type": item.record_type, "content": item.content}

            if existing is not None:
                same = (existing["occurred_at"] == item.occurred_at
                        and existing["record_type"] == item.record_type
                        and existing["content"] == item.content)
                if same:
                    # 幂等：内容完全一致，原样返回已存记录，不做任何写入
                    entry.update(status="duplicate", idempotent=True,
                                 message="内容一致，幂等返回",
                                 record_id=existing["id"],
                                 revision_no=existing["revision_no"])
                    duplicate += 1
                else:
                    # 冲突：保留已存内容不动，留存冲突证据
                    cid = new_id()
                    conn.execute(
                        "INSERT INTO conflicts (id, drill_id, terminal_id, record_id, "
                        "seq, existing, incoming, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (cid, drill_id, terminal["id"], existing["id"], item.seq,
                         json.dumps({"occurred_at": existing["occurred_at"],
                                     "record_type": existing["record_type"],
                                     "content": existing["content"]},
                                    ensure_ascii=False),
                         json.dumps(incoming, ensure_ascii=False), ts),
                    )
                    entry.update(status="conflict",
                                 message="同一序号已存在且内容不同，已留存冲突证据",
                                 record_id=existing["id"],
                                 conflict_id=cid,
                                 existing={"occurred_at": existing["occurred_at"],
                                           "record_type": existing["record_type"],
                                           "content": existing["content"]},
                                 incoming=incoming)
                    conflict += 1
            else:
                rid = new_id()
                conn.execute(
                    "INSERT INTO records (id, drill_id, terminal_id, seq, occurred_at, "
                    "record_type, content, revision_no, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (rid, drill_id, terminal["id"], item.seq, item.occurred_at,
                     item.record_type, item.content, ts, ts),
                )
                conn.execute(
                    "INSERT INTO record_revisions (id, record_id, revision_no, "
                    "occurred_at, record_type, content, reason, created_at) "
                    "VALUES (?, ?, 0, ?, ?, ?, NULL, ?)",
                    (new_id(), rid, item.occurred_at, item.record_type,
                     item.content, ts),
                )
                entry.update(status="accepted", message="已接收", record_id=rid,
                             revision_no=0)
                accepted += 1
            seen_in_batch.add(item.seq)
            results.append(entry)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "drill_id": drill_id,
        "terminal_code": terminal_code,
        "received_at": ts,
        "results": results,
        "summary": {
            "total": len(payload.records),
            "accepted": accepted,
            "duplicate": duplicate,
            "conflict": conflict,
            "rejected": rejected,
        },
    }


# ---------------------------------------------------------------- 时间线 / 单条记录


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/timeline")
def get_timeline(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    return build_timeline(conn, drill_id)


def _find_record(conn: sqlite3.Connection, drill_id: str,
                 terminal_code: str, seq: int):
    return conn.execute(
        "SELECT r.*, t.code AS terminal_code FROM records r "
        "JOIN terminals t ON t.id = r.terminal_id "
        "WHERE r.drill_id = ? AND t.code = ? AND r.seq = ?",
        (drill_id, terminal_code, seq),
    ).fetchone()


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/terminals/{{terminal_code}}/records/{{seq}}")
def get_record(drill_id: str, terminal_code: str, seq: int,
               conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    if get_terminal_or_none(conn, drill_id, terminal_code) is None:
        return error("terminal_not_registered", "终端未在本演练登记",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code)
    if seq < 1:
        return error("invalid_seq", "序号必须 >= 1", status.HTTP_400_BAD_REQUEST)
    row = _find_record(conn, drill_id, terminal_code, seq)
    if row is None:
        return error("record_not_found", f"序号 {seq} 的记录不存在",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code, seq=seq)
    return record_out(row, row["terminal_code"])


# ---------------------------------------------------------------- 更正与修订


@app.patch(f"{API_PREFIX}/drills/{{drill_id}}/terminals/{{terminal_code}}/records/{{seq}}")
def correct_record(drill_id: str, terminal_code: str, seq: int, payload: CorrectIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    if get_terminal_or_none(conn, drill_id, terminal_code) is None:
        return error("terminal_not_registered", "终端未在本演练登记",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code)
    if seq < 1:
        return error("invalid_seq", "序号必须 >= 1", status.HTTP_400_BAD_REQUEST)

    row = _find_record(conn, drill_id, terminal_code, seq)
    if row is None:
        return error("record_not_found", f"序号 {seq} 的记录不存在，无法更正",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code, seq=seq)

    new_occurred = payload.occurred_at if payload.occurred_at is not None else row["occurred_at"]
    new_type = payload.record_type if payload.record_type is not None else row["record_type"]
    new_content = payload.content if payload.content is not None else row["content"]

    if (new_occurred == row["occurred_at"] and new_type == row["record_type"]
            and new_content == row["content"]):
        return error("no_change", "更正内容与现有记录完全相同，未产生修订",
                     status.HTTP_400_BAD_REQUEST, record_id=row["id"])

    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = now_iso()
        new_rev = row["revision_no"] + 1
        conn.execute(
            "UPDATE records SET occurred_at = ?, record_type = ?, content = ?, "
            "revision_no = ?, updated_at = ? WHERE id = ?",
            (new_occurred, new_type, new_content, new_rev, ts, row["id"]),
        )
        conn.execute(
            "INSERT INTO record_revisions (id, record_id, revision_no, occurred_at, "
            "record_type, content, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), row["id"], new_rev, new_occurred, new_type, new_content,
             payload.reason, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    updated = conn.execute("SELECT * FROM records WHERE id = ?", (row["id"],)).fetchone()
    return {"status": "corrected", "record": record_out(updated, terminal_code),
            "reason": payload.reason, "corrected_at": ts}


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/terminals/{{terminal_code}}/records/{{seq}}/revisions")
def list_revisions(drill_id: str, terminal_code: str, seq: int,
                   conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    if get_terminal_or_none(conn, drill_id, terminal_code) is None:
        return error("terminal_not_registered", "终端未在本演练登记",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code)
    if seq < 1:
        return error("invalid_seq", "序号必须 >= 1", status.HTTP_400_BAD_REQUEST)
    row = _find_record(conn, drill_id, terminal_code, seq)
    if row is None:
        return error("record_not_found", f"序号 {seq} 的记录不存在",
                     status.HTTP_404_NOT_FOUND, terminal_code=terminal_code, seq=seq)
    revs = conn.execute(
        "SELECT * FROM record_revisions WHERE record_id = ? ORDER BY revision_no",
        (row["id"],),
    ).fetchall()
    return {
        "record_id": row["id"], "terminal_code": terminal_code, "seq": seq,
        "current_revision_no": row["revision_no"],
        "revisions": [{
            "revision_no": r["revision_no"],
            "occurred_at": r["occurred_at"],
            "record_type": r["record_type"],
            "content": r["content"],
            "reason": r["reason"],
            "created_at": r["created_at"],
            "is_original": r["revision_no"] == 0,
        } for r in revs],
    }


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/conflicts")
def list_conflicts(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    rows = conn.execute(
        "SELECT c.*, t.code AS terminal_code FROM conflicts c "
        "JOIN terminals t ON t.id = c.terminal_id "
        "WHERE c.drill_id = ? ORDER BY c.created_at, c.seq",
        (drill_id,),
    ).fetchall()
    return {"conflicts": [{
        "conflict_id": r["id"], "terminal_code": r["terminal_code"],
        "seq": r["seq"], "record_id": r["record_id"],
        "existing": json.loads(r["existing"]),
        "incoming": json.loads(r["incoming"]),
        "created_at": r["created_at"],
    } for r in rows]}


# ---------------------------------------------------------------- 确认丢失


@app.post(f"{API_PREFIX}/drills/{{drill_id}}/lost")
def confirm_lost(drill_id: str, payload: LostBatchIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    """逐号确认丢失以闭合缺号；逐条返回状态。"""
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)

    results: List[Dict[str, Any]] = []
    confirmed = duplicate = rejected = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = now_iso()
        seen: set = set()
        for idx, item in enumerate(payload.items):
            entry: Dict[str, Any] = {"index": idx, "terminal_code": item.terminal_code,
                                     "seq": item.seq}
            if item.seq < 1:
                entry.update(status="rejected", error_code="invalid_seq",
                             message="序号必须 >= 1")
                rejected += 1
                results.append(entry)
                continue
            key = (item.terminal_code, item.seq)
            if key in seen:
                entry.update(status="rejected", error_code="duplicate_in_batch",
                             message="同一批次内重复条目")
                rejected += 1
                results.append(entry)
                continue
            terminal = get_terminal_or_none(conn, drill_id, item.terminal_code)
            if terminal is None:
                entry.update(status="rejected", error_code="terminal_not_registered",
                             message="终端未在本演练登记")
                rejected += 1
                results.append(entry)
                continue
            existing_rec = conn.execute(
                "SELECT id FROM records WHERE terminal_id = ? AND seq = ?",
                (terminal["id"], item.seq),
            ).fetchone()
            if existing_rec is not None:
                entry.update(status="rejected", error_code="seq_has_record",
                             message="该序号已有上报记录，不能确认丢失",
                             record_id=existing_rec["id"])
                rejected += 1
                results.append(entry)
                continue
            existing_lost = conn.execute(
                "SELECT id FROM lost_sequences WHERE terminal_id = ? AND seq = ?",
                (terminal["id"], item.seq),
            ).fetchone()
            if existing_lost is not None:
                entry.update(status="duplicate", idempotent=True,
                             message="该序号此前已确认丢失，幂等返回",
                             lost_id=existing_lost["id"])
                duplicate += 1
                results.append(entry)
                continue
            lid = new_id()
            conn.execute(
                "INSERT INTO lost_sequences (id, drill_id, terminal_id, seq, reason, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lid, drill_id, terminal["id"], item.seq, item.reason, ts),
            )
            entry.update(status="confirmed", message="已确认丢失", lost_id=lid)
            confirmed += 1
            seen.add(key)
            results.append(entry)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "drill_id": drill_id, "confirmed_at": ts, "results": results,
        "summary": {"total": len(payload.items), "confirmed": confirmed,
                    "duplicate": duplicate, "rejected": rejected},
    }


# ---------------------------------------------------------------- 封存


@app.post(f"{API_PREFIX}/drills/{{drill_id}}/seals",
          status_code=status.HTTP_201_CREATED)
def seal_drill(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    drill = get_drill_or_none(conn, drill_id)
    if drill is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)

    try:
        conn.execute("BEGIN IMMEDIATE")
        timeline = build_timeline(conn, drill_id)
        if not timeline["summary"]["all_gaps_closed"]:
            conn.rollback()
            open_gaps = []
            for t in timeline["terminals"]:
                for m in t["missing"]:
                    if m["status"] == "open":
                        open_gaps.append({"terminal_code": t["terminal_code"],
                                          "seq": m["seq"]})
            return error("open_gaps_remain",
                         "仍有未闭合缺号：补齐上报或逐号确认丢失后方可封存",
                         status.HTTP_409_CONFLICT, open_gaps=open_gaps,
                         open_gap_count=len(open_gaps))

        prev = conn.execute(
            "SELECT MAX(version) AS v FROM seals WHERE drill_id = ?", (drill_id,)
        ).fetchone()["v"]
        version = (prev or 0) + 1
        sid, ts = new_id(), now_iso()
        snapshot = json.dumps(timeline, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "INSERT INTO seals (id, drill_id, version, snapshot, terminal_count, "
            "record_count, open_gap_count, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (sid, drill_id, version, snapshot,
             timeline["summary"]["terminal_count"],
             timeline["summary"]["record_count"], ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"seal_id": sid, "drill_id": drill_id, "version": version,
            "terminal_count": timeline["summary"]["terminal_count"],
            "record_count": timeline["summary"]["record_count"],
            "sealed_at": ts,
            "snapshot": timeline}


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/seals")
def list_seals(drill_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    rows = conn.execute(
        "SELECT id, drill_id, version, terminal_count, record_count, "
        "open_gap_count, created_at FROM seals WHERE drill_id = ? ORDER BY version",
        (drill_id,),
    ).fetchall()
    return {"seals": [dict(r) for r in rows]}


@app.get(f"{API_PREFIX}/drills/{{drill_id}}/seals/{{version}}")
def get_seal(drill_id: str, version: int, conn: sqlite3.Connection = Depends(get_conn)):
    if get_drill_or_none(conn, drill_id) is None:
        return error("drill_not_found", "演练不存在", status.HTTP_404_NOT_FOUND)
    row = conn.execute(
        "SELECT * FROM seals WHERE drill_id = ? AND version = ?",
        (drill_id, version),
    ).fetchone()
    if row is None:
        return error("seal_not_found", f"封存版本 {version} 不存在",
                     status.HTTP_404_NOT_FOUND, version=version)
    return {"seal_id": row["id"], "drill_id": drill_id,
            "version": row["version"],
            "terminal_count": row["terminal_count"],
            "record_count": row["record_count"],
            "sealed_at": row["created_at"],
            "snapshot": json.loads(row["snapshot"])}


# 全局未捕获异常时的兜底 JSON
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "error": {"code": "internal_error", "message": f"服务器内部错误: {exc}"}
    })
