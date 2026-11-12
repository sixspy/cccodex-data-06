"""运行后对账（reconciliation）。

对账只比较、只标记，**绝不修改正式采集结果** ``crawl_records``：

* 每次对账生成一个批次（``reconciliations``），差异写入
  ``reconciliation_diffs``，分为三类：
    - ``dir_only``       目录里有、库里没有（缺失，可补差的唯一来源）
    - ``db_only``        库里有、目录里没有（多出）
    - ``title_mismatch`` 两边都有、但标题不同
* 差异可被人工「保留」（必须填原因）。保留项之后每次对账仍会出现，
  但不允许用于生成补差。
* 补差任务（``catchup_tasks``）只能由**未保留的 ``dir_only``** 编码生成，
  且每个对账批次最多一条；补差运行期间拒绝再次触发。

条目的身份是 mock 条目的 ``sku``（编码）：目录侧取 ``sku``，库侧从已落库
记录的 ``payload_json`` 中解析 ``sku``，因此正式表结构无需改动。
"""

import json
import sqlite3

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page

DIR_ONLY = "dir_only"
DB_ONLY = "db_only"
TITLE_MISMATCH = "title_mismatch"

DIFF_LABELS = {
    DIR_ONLY: "目录有、库没有（缺失）",
    DB_ONLY: "库有、目录没有（多出）",
    TITLE_MISMATCH: "两边都有、标题不同",
}


# --------------------------------------------------------------------------- #
# 目录 / 库 两侧视图
# --------------------------------------------------------------------------- #
def _catalog_items(seed_path: str) -> list[dict]:
    """读取该种子目录当前看得到的条目（无外网，站内 mock）。"""
    if seed_path not in CATALOGS:
        raise ValueError("unknown seed")
    html = render_catalog_page(seed_path)
    if isinstance(html, tuple):
        html = html[0]
    return parse_catalog_html(html, seed_path)


def _db_items(job_id: int) -> list[dict]:
    """读取该任务当前正式结果，按编码（sku）去重视图。

    正式结果保持原样，这里只做只读解析。同一编码若有多条记录（理论上同
    sku 同标题去重后不会出现），保留第一条用于比对。
    """
    db = get_db()
    rows = db.execute(
        "SELECT title, payload_json FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    by_code: dict[str, dict] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            payload = {}
        code = payload.get("sku")
        if not code or code in by_code:
            continue
        by_code[code] = {"sku": code, "title": row["title"]}
    return list(by_code.values())


def compute_diff(job_id: int) -> list[dict]:
    """服务端计算三类差异（纯函数式只读，不落库、不改正式结果）。"""
    db = get_db()
    job = db.execute(
        "SELECT seed_path FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")

    catalog = {it["sku"]: it["title"] for it in _catalog_items(job["seed_path"])}
    library = {it["sku"]: it["title"] for it in _db_items(job_id)}

    diffs: list[dict] = []
    for code in sorted(catalog):
        if code not in library:
            diffs.append(
                {
                    "diff_type": DIR_ONLY,
                    "item_code": code,
                    "dir_title": catalog[code],
                    "db_title": None,
                }
            )
        elif catalog[code].strip() != library[code].strip():
            diffs.append(
                {
                    "diff_type": TITLE_MISMATCH,
                    "item_code": code,
                    "dir_title": catalog[code],
                    "db_title": library[code],
                }
            )
    for code in sorted(library):
        if code not in catalog:
            diffs.append(
                {
                    "diff_type": DB_ONLY,
                    "item_code": code,
                    "dir_title": None,
                    "db_title": library[code],
                }
            )
    return diffs


# --------------------------------------------------------------------------- #
# 对账批次
# --------------------------------------------------------------------------- #
def create_reconciliation(job_id: int) -> int:
    """对一个任务做一次对账，返回新批次 id。

    已被人工保留的差异（任意历史批次中）按 (diff_type, item_code) 继承到
    新批次，保证「下次对账还要出现，且仍不能用于补差」。本函数只写对账表，
    不触碰正式结果。
    """
    diffs = compute_diff(job_id)

    db = get_db()
    retained_map: dict[tuple[str, str], str] = {}
    prev = db.execute(
        """
        SELECT d.diff_type, d.item_code, d.retain_reason
        FROM reconciliation_diffs d
        JOIN reconciliations r ON r.id = d.reconciliation_id
        WHERE r.job_id = ? AND d.retained = 1
        """,
        (job_id,),
    ).fetchall()
    for row in prev:
        retained_map[(row["diff_type"], row["item_code"])] = row["retain_reason"]

    with db_transaction() as tx:
        cur = tx.execute(
            "INSERT INTO reconciliations (job_id) VALUES (?)", (job_id,)
        )
        recon_id = cur.lastrowid
        for d in diffs:
            key = (d["diff_type"], d["item_code"])
            reason = retained_map.get(key)
            is_retained = 1 if key in retained_map else 0
            tx.execute(
                """
                INSERT INTO reconciliation_diffs
                    (reconciliation_id, diff_type, item_code, dir_title, db_title,
                     retained, retain_reason, retained_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, {})
                """.format("datetime('now')" if is_retained else "NULL"),
                (
                    recon_id,
                    d["diff_type"],
                    d["item_code"],
                    d["dir_title"],
                    d["db_title"],
                    is_retained,
                    reason,
                ),
            )
    return recon_id


def get_reconciliation(recon_id: int):
    db = get_db()
    recon = db.execute(
        "SELECT * FROM reconciliations WHERE id = ?", (recon_id,)
    ).fetchone()
    if recon is None:
        return None, []
    diffs = db.execute(
        "SELECT * FROM reconciliation_diffs WHERE reconciliation_id = ? ORDER BY id",
        (recon_id,),
    ).fetchall()
    return recon, diffs


def latest_reconciliation(job_id: int):
    db = get_db()
    recon = db.execute(
        "SELECT * FROM reconciliations WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if recon is None:
        return None, []
    return get_reconciliation(recon["id"])


def retain_diff(recon_id: int, diff_id: int, reason: str) -> None:
    """把单条差异标记为人工保留，原因必填。"""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("保留时必须填写原因")
    db = get_db()
    with db_transaction() as tx:
        cur = tx.execute(
            """
            UPDATE reconciliation_diffs
            SET retained = 1,
                retain_reason = ?,
                retained_at = datetime('now')
            WHERE id = ? AND reconciliation_id = ?
            """,
            (reason, diff_id, recon_id),
        )
        if cur.rowcount == 0:
            raise ValueError("差异不存在或不属于该对账批次")


# --------------------------------------------------------------------------- #
# 补差任务
# --------------------------------------------------------------------------- #
def eligible_catchup_codes(recon_id: int) -> list[str]:
    """该批次可补差的编码：仅未保留的 dir_only，按编码排序。"""
    db = get_db()
    rows = db.execute(
        """
        SELECT item_code FROM reconciliation_diffs
        WHERE reconciliation_id = ? AND diff_type = ? AND retained = 0
        ORDER BY item_code
        """,
        (recon_id, DIR_ONLY),
    ).fetchall()
    return [r["item_code"] for r in rows]


def create_catchup(recon_id: int) -> dict:
    """为一个对账批次生成并执行补差任务。

    约束（任一不满足都拒绝）：
      * 批次存在且其任务已正常结束；
      * 同一批次尚未生成过补差（同批未处理完也不能再生成第二条）；
      * 已有补差正在运行时拒绝再次触发；
      * 存在未保留的 dir_only 编码（保留项与标题不同项都不能补差）。

    补差只抓允许的编码，目录里的其它编码不会被顺手抓进来。
    """
    db = get_db()
    recon = db.execute(
        "SELECT * FROM reconciliations WHERE id = ?", (recon_id,)
    ).fetchone()
    if recon is None:
        raise ValueError("对账批次不存在")

    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (recon["job_id"],)
    ).fetchone()
    if job is None:
        raise ValueError("任务不存在")
    if job["status"] not in ("done", "failed"):
        raise ValueError("任务尚未结束，不能补差")

    existing = db.execute(
        "SELECT id, status FROM catchup_tasks WHERE reconciliation_id = ?",
        (recon_id,),
    ).fetchone()
    if existing is not None:
        if existing["status"] == "running":
            raise ValueError("补差正在运行，不能再次触发")
        raise ValueError("该批次已生成过补差，同一批差异不能重复补差")

    codes = eligible_catchup_codes(recon_id)
    if not codes:
        raise ValueError("没有可补差的编码（缺失项可能已全部人工保留）")

    with db_transaction() as tx:
        cur = tx.execute(
            """
            INSERT INTO catchup_tasks
                (reconciliation_id, job_id, item_codes_json, status)
            VALUES (?, ?, ?, 'running')
            """,
            (recon_id, job["id"], json.dumps(codes, ensure_ascii=False)),
        )
        catchup_id = cur.lastrowid

    return _run_catchup(catchup_id)


def _run_catchup(catchup_id: int) -> dict:
    """执行补差：只遍历指定编码，抓一条写一条，随后自动再对账。"""
    db = get_db()
    task = db.execute(
        "SELECT * FROM catchup_tasks WHERE id = ?", (catchup_id,)
    ).fetchone()
    if task is None:
        raise ValueError("补差任务不存在")
    if task["status"] != "running":
        raise ValueError("补差任务不在运行中")

    codes = json.loads(task["item_codes_json"])
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (task["job_id"],)
    ).fetchone()
    items = _catalog_items(job["seed_path"])
    by_code = {it["sku"]: it for it in items}

    inserted = 0
    skipped_dup = 0
    missing_codes = 0
    with db_transaction() as tx:
        # 只遍历补差名单里的编码，绝不遍历整个目录，避免顺带抓其它编码。
        for code in codes:
            it = by_code.get(code)
            if it is None:
                # 目录里此刻已看不到该编码，无可抓内容，跳过。
                missing_codes += 1
                continue
            key = normalize_item_key(job["seed_path"], it["title"])
            try:
                tx.execute(
                    """
                    INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job["id"],
                        key,
                        it["title"],
                        json.dumps(it, ensure_ascii=False),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1
        tx.execute(
            """
            UPDATE catchup_tasks
            SET status = 'done', finished_at = datetime('now')
            WHERE id = ?
            """,
            (catchup_id,),
        )

    # 补差结束后自动再对一次账（同样只写对账表，不动既有正式结果）。
    new_recon_id = create_reconciliation(job["id"])
    return {
        "status": "done",
        "catchup_id": catchup_id,
        "codes": codes,
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "missing_codes": missing_codes,
        "new_reconciliation_id": new_recon_id,
    }
