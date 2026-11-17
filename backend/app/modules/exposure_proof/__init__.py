"""敞口配平证明：每个归并运行对每个被触及正向留下的不可只靠报告文案的落库证明。

每个归并运行（``settle_run_id``）对每个被触及的正向键写一行证明，记录：

* 正向键（``forward_entry_key``）与原金额；
* 配平前已冲累计 / 剩余敞口（取自配平开始时的物化敞口快照）；
* 配平后已冲累计 / 剩余敞口（取自配平结束时的 ``forward_exposures``，
  与敞口、流水、余额在同一事务内同源派生）；
* 本运行实际配平冲正额（``applied_cents``）；
* 该正向在本运行被闭进未平隔离的超额冲正笔数
  （``excess_quarantine_count``）。

证明只在归并事务内写入，只读接口对外提供，禁止任何改写路径。
"""

from __future__ import annotations

import sqlite3


def record(
    conn: sqlite3.Connection,
    settle_run_id: int,
    forward_entry_key: str,
    original_cents: int,
    before_reversed_cents: int,
    before_remaining_cents: int,
    after_reversed_cents: int,
    after_remaining_cents: int,
    applied_cents: int,
    excess_quarantine_count: int,
    now: str,
) -> None:
    """在调用方事务内追加一行配平证明（同运行同正向唯一）。"""
    conn.execute(
        """
        INSERT INTO exposure_settle_proofs(
          settle_run_id, forward_entry_key, original_cents,
          before_reversed_cents, before_remaining_cents,
          after_reversed_cents, after_remaining_cents,
          applied_cents, excess_quarantine_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settle_run_id,
            forward_entry_key,
            original_cents,
            before_reversed_cents,
            before_remaining_cents,
            after_reversed_cents,
            after_remaining_cents,
            applied_cents,
            excess_quarantine_count,
            now,
        ),
    )


def list_for_run(conn: sqlite3.Connection, settle_run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM exposure_settle_proofs
        WHERE settle_run_id = ?
        ORDER BY id
        """,
        (settle_run_id,),
    ).fetchall()


def proof_mismatch_count(conn: sqlite3.Connection, settle_run_id: int) -> int:
    """证明守恒校验（供互证报告使用）。

    1. 证明内部守恒：配平后已冲 = 配平前已冲 + 实际配平额；
       配平前/后剩余 = 原额 − 对应已冲累计，且不小于 0。
    2. 最新轮证明的配平后口径须与当前物化敞口表逐分一致。
    3. 证明登记的超额隔离笔数须与未平隔离表实际记录一致。
    """
    latest = conn.execute("SELECT MAX(id) AS m FROM settle_runs").fetchone()["m"]
    bad = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM exposure_settle_proofs
        WHERE settle_run_id = ?
          AND (
                after_reversed_cents != before_reversed_cents + applied_cents
             OR before_remaining_cents != original_cents - before_reversed_cents
             OR after_remaining_cents != original_cents - after_reversed_cents
             OR before_remaining_cents < 0
             OR after_remaining_cents < 0
          )
        """,
        (settle_run_id,),
    ).fetchone()["c"]
    if settle_run_id == latest:
        bad += conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM exposure_settle_proofs p
            JOIN forward_exposures fe ON fe.forward_entry_key = p.forward_entry_key
            WHERE p.settle_run_id = ?
              AND (p.after_reversed_cents != fe.reversed_cents
                   OR p.after_remaining_cents != fe.remaining_cents)
            """,
            (settle_run_id,),
        ).fetchone()["c"]
    bad += conn.execute(
        """
        SELECT COUNT(*) AS c FROM (
          SELECT p.forward_entry_key,
                 p.excess_quarantine_count AS p_count,
                 COALESCE((
                   SELECT COUNT(*) FROM quarantine_items q
                   WHERE q.settle_run_id = p.settle_run_id
                     AND q.target_entry_key = p.forward_entry_key
                     AND q.reason = '冲正超额'
                 ), 0) AS q_count
          FROM exposure_settle_proofs p
          WHERE p.settle_run_id = ?
        ) WHERE p_count != q_count
        """,
        (settle_run_id,),
    ).fetchone()["c"]
    return bad
