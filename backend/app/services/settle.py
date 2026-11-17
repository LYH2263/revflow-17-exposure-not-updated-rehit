from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules import exposure_proof, partial_cascade


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        snap = partial_cascade.snapshot(conn)
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "已冲累计分": snap["reversed_total_cents"],
            "剩余敞口分": snap["open_cents"],
            "闭口正向数": snap["closed_count"],
            "部分冲正向数": snap["partial_count"],
            "敞口不符数": partial_cascade.mismatch_count(conn),
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    """流水列表；正向附带物化敞口与最近一次配平证明（同源派生，刷新后仍在）。"""
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name,
                       fe.original_cents AS exposure_original_cents,
                       fe.reversed_cents AS exposure_reversed_cents,
                       fe.remaining_cents AS exposure_remaining_cents,
                       fe.state AS exposure_state,
                       fe.last_reversal_key AS exposure_last_reversal_key,
                       p.settle_run_id AS proof_run_id,
                       p.before_remaining_cents AS proof_before_remaining_cents,
                       p.after_remaining_cents AS proof_after_remaining_cents,
                       p.applied_cents AS proof_applied_cents
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                LEFT JOIN forward_exposures fe
                  ON e.kind = '正向' AND fe.forward_entry_key = e.entry_key
                LEFT JOIN exposure_settle_proofs p
                  ON e.kind = '正向'
                 AND p.forward_entry_key = e.entry_key
                 AND p.id = (
                       SELECT MAX(id) FROM exposure_settle_proofs
                       WHERE forward_entry_key = e.entry_key
                   )
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name,
                       fe.original_cents AS exposure_original_cents,
                       fe.reversed_cents AS exposure_reversed_cents,
                       fe.remaining_cents AS exposure_remaining_cents,
                       fe.state AS exposure_state,
                       fe.last_reversal_key AS exposure_last_reversal_key,
                       p.settle_run_id AS proof_run_id,
                       p.before_remaining_cents AS proof_before_remaining_cents,
                       p.after_remaining_cents AS proof_after_remaining_cents,
                       p.applied_cents AS proof_applied_cents
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                LEFT JOIN forward_exposures fe
                  ON e.kind = '正向' AND fe.forward_entry_key = e.entry_key
                LEFT JOIN exposure_settle_proofs p
                  ON e.kind = '正向'
                 AND p.forward_entry_key = e.entry_key
                 AND p.id = (
                       SELECT MAX(id) FROM exposure_settle_proofs
                       WHERE forward_entry_key = e.entry_key
                   )
                ORDER BY e.id
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_exposures() -> list[dict]:
    """正向剩余敞口明细（持久物化，刷新后仍在）。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              fe.forward_entry_key,
              e.account_id,
              a.code AS account_code,
              e.amount_cents AS original_amount_cents,
              fe.original_cents,
              fe.reversed_cents,
              fe.remaining_cents,
              fe.state,
              fe.last_reversal_key,
              fe.last_settle_run_id,
              fe.updated_at
            FROM forward_exposures fe
            JOIN ledger_entries e
              ON e.kind = '正向' AND e.entry_key = fe.forward_entry_key
            JOIN accounts a ON a.id = e.account_id
            ORDER BY e.id
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine() -> list[dict]:
    """未平隔离；超额项附带当轮该正向的配平证明（敞口前/后、配平额）。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code,
                   p.before_remaining_cents AS proof_before_remaining_cents,
                   p.after_remaining_cents AS proof_after_remaining_cents,
                   p.applied_cents AS proof_applied_cents,
                   p.excess_quarantine_count AS proof_excess_quarantine_count
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            LEFT JOIN exposure_settle_proofs p
              ON p.settle_run_id = q.settle_run_id
             AND p.forward_entry_key = q.target_entry_key
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_exposure_proofs(settle_run_id: int) -> list[dict]:
    """只读：按归并运行 id 查敞口配平证明。"""
    with connect() as conn:
        run = conn.execute(
            "SELECT id FROM settle_runs WHERE id = ?", (settle_run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")
        return rows_to_dicts(exposure_proof.list_for_run(conn, settle_run_id))


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def run_settle() -> dict:
    """一轮净额归并：敞口扣减 / 隔离 / 流水状态 / 余额 / 配平证明同一事务成败。

    失败闭路径（择一实现，此处为「先校验物化敞口再同事务改余额/敞口/隔离」）：
    冲正额度只取自持久化的 ``forward_exposures.remaining_cents``；新冲正超过
    当前物化敞口时，在任何余额/敞口写入**之前**整笔判失败，闭进未平隔离并
    写明当时敞口与超额，绝不先扣余额再改状态，也不做部分扣账。
    余额由「正式已归并流水」全量派生后覆盖写，保证重复跑不重复扣。
    每个被触及正向写一行敞口配平证明，挂本运行 id，与敞口/流水/余额同源。
    """
    conn = connect()
    try:
        now = _now()
        # 单事务提交：余额 / 敞口 / 隔离 / 流水状态 / 证明同成败
        conn.execute("BEGIN IMMEDIATE")
        partial_cascade.ensure_rows(conn, now)

        cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at) VALUES ('进行中', ?)",
            (now,),
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        # 本轮正向并入候选：允许冲正命中早前轮次已归并的正向，额度仍以敞口表为准
        all_forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        fwd_by_key = {f["entry_key"]: f for f in all_forwards}

        # 配平前物化敞口快照：证明的 before 口径，也是超额校验的唯一额度来源
        before_snapshot = {
            row["forward_entry_key"]: dict(row)
            for row in conn.execute("SELECT * FROM forward_exposures").fetchall()
        }
        # 本轮被触及正向：实际配平额 / 超额隔离笔数，落证明用
        applied_by_key: dict[str, int] = {}
        excess_by_key: dict[str, int] = {}

        touched_accounts: set[int] = {f["account_id"] for f in forwards}

        def quarantine(r: dict, reason: str, excess: int = 0, exposure: int = 0) -> None:
            conn.execute(
                """
                INSERT INTO quarantine_items(
                  entry_key, account_id, reason, amount_cents, settle_run_id,
                  created_at, target_entry_key, excess_cents, exposure_cents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["entry_key"],
                    r["account_id"],
                    reason,
                    r["amount_cents"],
                    run_id,
                    now,
                    r.get("target_entry_key"),
                    excess,
                    exposure,
                ),
            )
            conn.execute(
                "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )

        for r in reversals:
            target = r.get("target_entry_key")
            if not target or target not in fwd_by_key:
                quarantine(r, "冲正无原单")
                continue

            fwd = fwd_by_key[target]
            if fwd["account_id"] != r["account_id"]:
                quarantine(r, "冲正账户与原单不一致")
                continue

            # 先校验物化敞口，再在同事务内改敞口；超额在任何写入前整笔失败
            result = partial_cascade.apply(
                conn,
                forward_entry_key=target,
                amount_cents=r["amount_cents"],
                reversal_entry_key=r["entry_key"],
                settle_run_id=run_id,
                now=now,
            )
            if result.get("missing"):
                # 敞口行缺失：保守闭口，不猜额度，整笔隔离
                quarantine(r, "正向敞口缺失")
                continue
            if result.get("excess"):
                # 超额整笔失败：apply 未写任何敞口，正式余额更未动；写明当时敞口
                quarantine(
                    r,
                    partial_cascade.EXCESS_REASON,
                    excess=result["excess_cents"],
                    exposure=result["exposure_cents"],
                )
                excess_by_key[target] = excess_by_key.get(target, 0) + 1
                continue
            touched_accounts.add(r["account_id"])
            applied_by_key[target] = applied_by_key.get(target, 0) + r["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )

        # 余额全量派生：正式已归并正向 − 正式已归并冲正，按账户覆盖写。
        # 超额冲正状态为「已隔离」，天然不进余额；禁止先扣余额再改状态。
        bal_rows = conn.execute(
            """
            SELECT account_id,
                   COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents
                                     WHEN kind = '冲正' THEN -amount_cents
                                     ELSE 0 END), 0) AS net
            FROM ledger_entries
            WHERE status = '已归并'
            GROUP BY account_id
            """
        ).fetchall()
        account_net = {row["account_id"]: row["net"] for row in bal_rows}
        for acc_id in sorted(touched_accounts | set(account_net)):
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
                (account_net.get(acc_id, 0), run_id, acc_id),
            )

        # 敞口配平证明：每个被触及正向一行，before 取运行前快照，after 取当前物化行
        touched_keys = set(applied_by_key) | set(excess_by_key)
        after_snapshot = {
            row["forward_entry_key"]: dict(row)
            for row in conn.execute("SELECT * FROM forward_exposures").fetchall()
        }
        for key in sorted(touched_keys):
            before = before_snapshot[key]
            after = after_snapshot[key]
            exposure_proof.record(
                conn,
                settle_run_id=run_id,
                forward_entry_key=key,
                original_cents=after["original_cents"],
                before_reversed_cents=before["reversed_cents"],
                before_remaining_cents=before["remaining_cents"],
                after_reversed_cents=after["reversed_cents"],
                after_remaining_cents=after["remaining_cents"],
                applied_cents=applied_by_key.get(key, 0),
                excess_quarantine_count=excess_by_key.get(key, 0),
                now=now,
            )
        proof_count = len(touched_keys)

        # 报告口径全部从敞口表 / 流水状态 / 隔离表 / 账户余额 / 证明表派生，禁止内存计数
        snap = partial_cascade.snapshot(conn)
        mismatch = partial_cascade.mismatch_count(conn)
        proof_mismatch = exposure_proof.proof_mismatch_count(conn, run_id)
        counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM ledger_entries
                WHERE status = '已归并' AND settle_run_id = ?) AS settled_count,
              (SELECT COUNT(*) FROM quarantine_items
                WHERE settle_run_id = ?) AS quarantine_count,
              (SELECT COUNT(*) FROM quarantine_items
                WHERE settle_run_id = ? AND reason = ?) AS excess_count,
              (SELECT COALESCE(SUM(balance_cents), 0) FROM accounts
                WHERE last_settle_run_id = ?) AS net_cents
            """,
            (
                run_id,
                run_id,
                run_id,
                partial_cascade.EXCESS_REASON,
                run_id,
            ),
        ).fetchone()
        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?,
                reversed_total_cents = ?,
                open_cents = ?,
                closed_count = ?,
                excess_quarantine_count = ?,
                exposure_mismatch_count = ?,
                proof_count = ?,
                commit_mode = '单事务',
                note = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                counts["settled_count"],
                counts["quarantine_count"],
                counts["net_cents"],
                finished,
                snap["reversed_total_cents"],
                snap["open_cents"],
                snap["closed_count"],
                counts["excess_count"],
                mismatch + proof_mismatch,
                proof_count,
                "敞口/余额/隔离/流水状态/配平证明同事务提交；超额先校验后闭隔离",
                run_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        # 账户余额合计可能含历史；底座约定：互证看「本轮触及账户余额合计」vs 本轮流水净额
        # 简化：用全部账户余额 vs 全部已归并净额（多轮后仍应一致若每轮覆盖写）
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        # 敞口口径：全部从敞口表 / 隔离表 / 流水状态 / 证明表派生，禁止内存计数
        snap = partial_cascade.snapshot(conn)
        exposure_mismatch = partial_cascade.mismatch_count(conn)
        proof_mismatch = exposure_proof.proof_mismatch_count(conn, settle_run_id)
        proof_rows = exposure_proof.list_for_run(conn, settle_run_id)
        excess_q = conn.execute(
            """
            SELECT COUNT(*) AS c FROM quarantine_items
            WHERE settle_run_id = ? AND reason = ?
            """,
            (settle_run_id, partial_cascade.EXCESS_REASON),
        ).fetchone()["c"]
        latest = conn.execute("SELECT MAX(id) AS m FROM settle_runs").fetchone()["m"]
        # 该轮落库的敞口派生值；仅对最新轮与当前敞口表逐分核对（旧轮为历史快照）
        run_snapshot_matches = (
            run["reversed_total_cents"] == snap["reversed_total_cents"]
            and run["open_cents"] == snap["open_cents"]
            and run["excess_quarantine_count"] == excess_q
            if settle_run_id == latest
            else True
        )
        # 证明登记的超额笔数合计 = 隔离表实际超额笔数
        proof_excess_total = sum(p["excess_quarantine_count"] for p in proof_rows)
        proof_excess_matches = proof_excess_total == excess_q
        # 敞口守恒：原额 − 已冲累计 = 剩余敞口，且已冲累计与已归并冲正同额；
        # 配平证明守恒且与当前物化敞口同源
        consistent = (
            1
            if all_ledger_net == all_bal
            and exposure_mismatch == 0
            and proof_mismatch == 0
            and proof_excess_matches
            and snap["reversed_total_cents"] == all_rev
            and run_snapshot_matches
            else 0
        )
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "已冲累计分": run["reversed_total_cents"],
            "剩余敞口合计分": run["open_cents"],
            "当前剩余敞口合计分": snap["open_cents"],
            "闭口正向数": snap["closed_count"],
            "部分冲正向数": snap["partial_count"],
            "敞口不符数": exposure_mismatch,
            "本轮超额隔离笔数": excess_q,
            "最新轮敞口一致": 1 if run_snapshot_matches else 0,
            "配平证明笔数": len(proof_rows),
            "证明不符数": proof_mismatch,
            "证明超额笔数": proof_excess_total,
            "证明超额一致": 1 if proof_excess_matches else 0,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
