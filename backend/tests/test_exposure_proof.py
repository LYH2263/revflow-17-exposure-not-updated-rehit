"""敞口配平证明实体与「超额偷吃 / 敞口不更新重扣」回归的基线外测试。

覆盖：
* 证明按归并运行 id 的只读接口、字段完整性与守恒；
* 超额冲正整笔失败：不动正式余额、不动物化敞口，隔离写明当时敞口；
* 成功配平后剩余敞口 = 原金额 − 已成功冲正累计，敞口为 0 可观察为已闭口；
* 多轮重跑只按物化剩余扣，不按原金额重扣；
* 证明与敞口、流水、余额同源派生（互证报告纳入证明校验）；
* 证明只读：无 POST/PUT/DELETE 路径。
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(scope="module")
def _db(db_file):
    def _open() -> sqlite3.Connection:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        return conn

    return _open


def _insert_reversal(conn, id_, key, account, amount, target, at, note=""):
    conn.execute(
        """
        INSERT INTO ledger_entries(
          id, entry_key, account_id, kind, amount_cents, biz_ref,
          target_entry_key, booked_at, status, note
        ) VALUES (?, ?, ?, '冲正', ?, 'ORD-T', ?, ?, '待归并', ?)
        """,
        (id_, key, account, amount, target, at, note),
    )
    conn.commit()


def test_first_run_writes_proofs_for_every_touched_forward(client):
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    run_id = run["id"]

    proofs = client.get(f"/api/settle-runs/{run_id}/exposure-proofs").json()
    # 首轮触及：FWD-001/002/003/005 成功配平，FWD-003 另有一笔超额
    keys = {p["forward_entry_key"] for p in proofs}
    assert keys == {
        "FWD-20261116-001",
        "FWD-20261116-002",
        "FWD-20261116-003",
        "FWD-20261116-005",
    }
    by_key = {p["forward_entry_key"]: p for p in proofs}

    # 证明字段齐全：正向键、配平前/后敞口、隔离超额笔数、挂归并运行 id
    p2 = by_key["FWD-20261116-002"]
    assert p2["settle_run_id"] == run_id
    assert p2["original_cents"] == 25000
    assert p2["before_reversed_cents"] == 0
    assert p2["before_remaining_cents"] == 25000
    assert p2["after_reversed_cents"] == 5000
    assert p2["after_remaining_cents"] == 20000
    assert p2["applied_cents"] == 5000
    assert p2["excess_quarantine_count"] == 0
    # 证明内部守恒
    assert p2["after_reversed_cents"] == p2["before_reversed_cents"] + p2["applied_cents"]
    assert p2["after_remaining_cents"] == p2["original_cents"] - p2["after_reversed_cents"]

    # 闭口正向：剩余为 0 且状态可观察
    p1 = by_key["FWD-20261116-001"]
    assert p1["after_remaining_cents"] == 0
    exp = {e["forward_entry_key"]: e for e in client.get("/api/exposures").json()}
    assert exp["FWD-20261116-001"]["remaining_cents"] == 0
    assert exp["FWD-20261116-001"]["state"] == "已闭口"

    # FWD-003：先全额冲闭口，再一笔 500 超额；证明登记超额笔数 1
    p3 = by_key["FWD-20261116-003"]
    assert p3["after_remaining_cents"] == 0
    assert p3["applied_cents"] == 18000
    assert p3["excess_quarantine_count"] == 1

    # 运行行落证明笔数
    assert run["proof_count"] == len(proofs)


def test_proof_endpoint_is_read_only_and_scoped_by_run(client):
    # 未知运行 id → 404
    resp = client.get("/api/settle-runs/99999/exposure-proofs")
    assert resp.status_code == 404
    # 只读：不存在写接口
    assert client.post("/api/settle-runs/1/exposure-proofs").status_code in (404, 405)
    assert client.put("/api/settle-runs/1/exposure-proofs").status_code in (404, 405)
    assert client.delete("/api/settle-runs/1/exposure-proofs").status_code in (404, 405)
    # openapi 中该路径只有 GET
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths["/api/settle-runs/{run_id}/exposure-proofs"]) == {"get"}


def test_excess_does_not_touch_balance_or_exposure(client, _db):
    """超额整笔失败：正式余额与物化敞口都不动，隔离写明当时敞口。"""
    before_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    before_exp = {
        e["forward_entry_key"]: (e["reversed_cents"], e["remaining_cents"], e["state"])
        for e in client.get("/api/exposures").json()
    }

    conn = _db()
    try:
        _insert_reversal(
            conn, 910, "REV-PROOF-EXCESS", 1, 20001,
            "FWD-20261116-002", "2026-11-16T19:30:00Z", "超剩余 1 分",
        )
    finally:
        conn.close()

    run = client.post("/api/settle-runs").json()
    assert run["excess_quarantine_count"] == 1
    assert run["exposure_mismatch_count"] == 0

    q = [x for x in client.get("/api/quarantine").json() if x["entry_key"] == "REV-PROOF-EXCESS"]
    assert len(q) == 1
    item = q[0]
    assert item["reason"] == "冲正超额"
    assert item["excess_cents"] == 1
    # 当时物化敞口 = 20000，必须写进隔离
    assert item["exposure_cents"] == 20000

    after_bal = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    after_exp = {
        e["forward_entry_key"]: (e["reversed_cents"], e["remaining_cents"], e["state"])
        for e in client.get("/api/exposures").json()
    }
    assert after_bal == before_bal
    assert after_exp == before_exp

    # 该轮证明：FWD-002 仅登记超额，未实际配平，前/后敞口不变
    proofs = {
        p["forward_entry_key"]: p
        for p in client.get(f"/api/settle-runs/{run['id']}/exposure-proofs").json()
    }
    p = proofs["FWD-20261116-002"]
    assert p["applied_cents"] == 0
    assert p["excess_quarantine_count"] == 1
    assert p["before_remaining_cents"] == p["after_remaining_cents"] == 20000


def test_second_round_deducts_only_materialized_remainder(client, _db):
    """再冲 20000 打剩余 20000 的正向：恰好闭口，下一轮不得按原金额重扣。"""
    conn = _db()
    try:
        _insert_reversal(
            conn, 911, "REV-PROOF-CLOSE", 1, 20000,
            "FWD-20261116-002", "2026-11-16T20:30:00Z", "补冲闭口",
        )
    finally:
        conn.close()

    close_run = client.post("/api/settle-runs").json()
    assert close_run["exposure_mismatch_count"] == 0
    exp = {e["forward_entry_key"]: e for e in client.get("/api/exposures").json()}
    fwd2 = exp["FWD-20261116-002"]
    # 剩余敞口 = 原金额 − 已成功冲正累计 = 25000 − (5000 + 20000) = 0
    assert fwd2["reversed_cents"] == 25000
    assert fwd2["remaining_cents"] == 0
    assert fwd2["state"] == "已闭口"

    proofs = {
        p["forward_entry_key"]: p
        for p in client.get(f"/api/settle-runs/{close_run['id']}/exposure-proofs").json()
    }
    p = proofs["FWD-20261116-002"]
    assert p["before_remaining_cents"] == 20000
    assert p["after_remaining_cents"] == 0
    assert p["applied_cents"] == 20000

    # 空跑一轮：无待归并，余额/敞口/证明数都不变，不按原金额重扣
    bal_before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    rerun = client.post("/api/settle-runs").json()
    assert rerun["forward_count"] == 0
    assert rerun["reversal_count"] == 0
    assert rerun["proof_count"] == 0
    bal_after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert bal_after == bal_before

    # 闭口后再冲任意金额：整笔隔离，余额不动
    conn = _db()
    try:
        _insert_reversal(
            conn, 912, "REV-PROOF-AFTER-CLOSE", 1, 1,
            "FWD-20261116-002", "2026-11-16T21:30:00Z", "闭口后再冲",
        )
    finally:
        conn.close()
    after_close_run = client.post("/api/settle-runs").json()
    assert after_close_run["excess_quarantine_count"] == 1
    bal_final = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    assert bal_final == bal_before
    q = [x for x in client.get("/api/quarantine").json() if x["entry_key"] == "REV-PROOF-AFTER-CLOSE"]
    assert q[0]["exposure_cents"] == 0
    assert q[0]["excess_cents"] == 1


def test_proofs_persist_across_reconnect_and_feed_verify(client, _db):
    # 重开连接直读证明表：持久化而非请求内变量
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT settle_run_id, forward_entry_key, after_remaining_cents "
            "FROM exposure_settle_proofs ORDER BY id"
        ).fetchall()
        assert rows
        latest_run = conn.execute("SELECT MAX(id) AS m FROM settle_runs").fetchone()["m"]
    finally:
        conn.close()
    assert any(r["settle_run_id"] == latest_run for r in rows)

    # 互证报告把证明纳入同源校验
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    detail = report["detail"]
    assert detail["证明不符数"] == 0
    assert detail["证明超额一致"] == 1
    assert detail["配平证明笔数"] > 0
    assert detail["证明超额笔数"] == detail["本轮超额隔离笔数"]


def test_forward_entries_and_quarantine_surface_proof_fields(client):
    rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=正向").json()}
    fwd2 = rows["FWD-20261116-002"]
    # 最近一次配平证明随正向流水下发：FWD-002 末次被触及是闭口后超额那轮，前/后敞口均为 0
    assert fwd2["proof_run_id"] is not None
    assert fwd2["proof_before_remaining_cents"] == 0
    assert fwd2["proof_after_remaining_cents"] == 0
    assert fwd2["proof_applied_cents"] == 0
    assert fwd2["exposure_remaining_cents"] == 0
    assert fwd2["exposure_state"] == "已闭口"

    # 历史证明链仍可按运行 id 只读取回：补冲闭口那轮 before=20000 / after=0
    all_runs = client.get("/api/settle-runs").json()
    close_run = next(
        r for r in all_runs
        if any(
            p["forward_entry_key"] == "FWD-20261116-002"
            and p["before_remaining_cents"] == 20000
            and p["after_remaining_cents"] == 0
            for p in client.get(f"/api/settle-runs/{r['id']}/exposure-proofs").json()
        )
    )
    assert close_run["id"] > 0

    q = [x for x in client.get("/api/quarantine").json() if x["entry_key"] == "REV-PROOF-AFTER-CLOSE"]
    item = q[0]
    # 隔离界面可看到当轮证明口径
    assert item["proof_before_remaining_cents"] == 0
    assert item["proof_after_remaining_cents"] == 0
    assert item["proof_applied_cents"] == 0
    assert item["proof_excess_quarantine_count"] == 1


def test_failed_round_rolls_back_proofs_too(client, _db):
    """事务后期失败：证明与敞口/余额/隔离一并回滚。"""
    conn = _db()
    try:
        _insert_reversal(
            conn, 913, "REV-PROOF-ROLLBACK", 3, 1000,
            "FWD-20261116-004", "2026-11-16T22:00:00Z", "触发回滚",
        )
        before_proofs = conn.execute("SELECT COUNT(*) AS c FROM exposure_settle_proofs").fetchone()["c"]
        conn.execute(
            """
            CREATE TRIGGER fail_balance_update_proof
            BEFORE UPDATE OF balance_cents ON accounts
            BEGIN
              SELECT RAISE(ABORT, '模拟过账失败');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception):
        client.post("/api/settle-runs")

    conn = _db()
    try:
        after_proofs = conn.execute("SELECT COUNT(*) AS c FROM exposure_settle_proofs").fetchone()["c"]
        status = conn.execute(
            "SELECT status FROM ledger_entries WHERE entry_key = 'REV-PROOF-ROLLBACK'"
        ).fetchone()["status"]
        exp = conn.execute(
            "SELECT remaining_cents FROM forward_exposures WHERE forward_entry_key = 'FWD-20261116-004'"
        ).fetchone()["remaining_cents"]
    finally:
        conn.close()
    assert after_proofs == before_proofs
    assert status == "待归并"
    assert exp == 50000

    conn = _db()
    try:
        conn.execute("DROP TRIGGER IF EXISTS fail_balance_update_proof")
        conn.commit()
    finally:
        conn.close()
