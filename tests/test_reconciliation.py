"""运行后对账 / 人工保留 / 补差 的自动化测试。

约定的七组核心场景（与题目逐条对应）：
  1. test_three_diff_types_presented_separately  三类差异都会出现、分开展示
  2. test_retained_missing_cannot_catchup        保留之后不能生成补差
  3. test_unretained_missing_can_catchup         未保留的缺失可以生成补差
  4. test_catchup_contains_only_specified_codes  补差只含指定编码
  5. test_same_batch_cannot_catchup_twice        同一批差异不能生成两次
  6. test_title_mismatch_never_enters_catchup    标题不同不会进入补差
  7. test_catchup_running_blocks_second_trigger  运行尚未结束时拒绝再次触发
"""

import json

import pytest

from app.db import get_db
from app.services import reconciliation as recon
from app.services.runner import run_job

GAMMA = "/mock/catalog/gamma"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_gamma_job(app, client, name="gamma-job"):
    login(client)
    client.post(
        "/jobs",
        data={"name": name, "seed_path": GAMMA},
        follow_redirects=True,
    )
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def seed_records(app, job_id, rows):
    """直接写正式结果并把任务标记为已结束，构造库侧任意视图。

    rows: [(sku, title), ...]。补差只允许在已结束的任务上进行，因此这里
    一并把任务置为 done（等价于一次正常运行结束）。
    """
    from app.services.dedup import normalize_item_key

    with app.app_context():
        db = get_db()
        for sku, title in rows:
            db.execute(
                """
                INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    job_id,
                    normalize_item_key(GAMMA, title),
                    title,
                    json.dumps({"sku": sku, "title": title, "seed_path": GAMMA}),
                ),
            )
        db.execute(
            "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') "
            "WHERE id = ?",
            (job_id,),
        )
        db.commit()


def diffs_by_type(app, recon_id):
    with app.app_context():
        _, diffs = recon.get_reconciliation(recon_id)
    grouped = {recon.DIR_ONLY: [], recon.DB_ONLY: [], recon.TITLE_MISMATCH: []}
    for d in diffs:
        grouped[d["diff_type"]].append(d)
    return grouped


def make_recon(app, job_id):
    with app.app_context():
        return recon.create_reconciliation(job_id)


def retain(app, recon_id, diff_id, reason="人工确认无需补齐"):
    with app.app_context():
        recon.retain_diff(recon_id, diff_id, reason)


# --------------------------------------------------------------------------- #
# 1. 三类差异都会出现，并分三类展示
# --------------------------------------------------------------------------- #
def test_three_diff_types_presented_separately(app, client):
    job_id = create_gamma_job(app, client)
    # 库侧：G-001 正常；G-002 标题不同；G-003/G-004 缺失；X-999 库里多出。
    seed_records(
        app,
        job_id,
        [
            ("G-001", "管网压力监测点台账"),
            ("G-002", "旧版河道标题（已改名）"),
            ("X-999", "目录已下线的历史条目"),
        ],
    )
    recon_id = make_recon(app, job_id)
    grouped = diffs_by_type(app, recon_id)

    dir_only = [d["item_code"] for d in grouped[recon.DIR_ONLY]]
    db_only = [d["item_code"] for d in grouped[recon.DB_ONLY]]
    mismatch = [d["item_code"] for d in grouped[recon.TITLE_MISMATCH]]

    assert sorted(dir_only) == ["G-003", "G-004"]
    assert db_only == ["X-999"]
    assert mismatch == ["G-002"]

    # 页面分三类、各自独立分区，且可按类型筛选。
    html = client.get(
        f"/jobs/{job_id}/reconciliation?recon={recon_id}"
    ).get_data(as_text=True)
    assert "目录有、库没有（缺失）" in html
    assert "库有、目录没有（多出）" in html
    assert "两边都有、标题不同" in html

    filtered = client.get(
        f"/jobs/{job_id}/reconciliation?recon={recon_id}&type=dir_only"
    ).get_data(as_text=True)
    assert "G-003" in filtered and "G-004" in filtered
    assert "X-999" not in filtered  # 其它类型被筛掉


# --------------------------------------------------------------------------- #
# 2. 保留之后不能生成补差（保留项下次对账仍出现）
# --------------------------------------------------------------------------- #
def test_retained_missing_cannot_catchup(app, client):
    job_id = create_gamma_job(app, client)
    seed_records(app, job_id, [("G-001", "管网压力监测点台账")])
    recon_id = make_recon(app, job_id)
    grouped = diffs_by_type(app, recon_id)
    missing = {d["item_code"]: d["id"] for d in grouped[recon.DIR_ONLY]}
    assert sorted(missing) == ["G-002", "G-003", "G-004"]

    # 把全部缺失项人工保留。
    for diff_id in missing.values():
        retain(app, recon_id, diff_id)

    with app.app_context():
        assert recon.eligible_catchup_codes(recon_id) == []
        with pytest.raises(ValueError, match="没有可补差的编码"):
            recon.create_catchup(recon_id)

    # 保留原因必填。
    with app.app_context():
        fresh_id = recon.create_reconciliation(job_id)
    grouped2 = diffs_by_type(app, fresh_id)
    retained_rows = [d for d in grouped2[recon.DIR_ONLY] if d["retained"]]
    # 已保留条目在新批次里仍然出现，且带着保留原因。
    assert sorted(d["item_code"] for d in retained_rows) == [
        "G-002",
        "G-003",
        "G-004",
    ]
    assert all(d["retain_reason"] for d in retained_rows)
    with app.app_context():
        with pytest.raises(ValueError, match="原因"):
            recon.retain_diff(fresh_id, grouped2[recon.DIR_ONLY][0]["id"], "  ")


# --------------------------------------------------------------------------- #
# 3. 未保留的缺失可以生成补差
# --------------------------------------------------------------------------- #
def test_unretained_missing_can_catchup(app, client):
    job_id = create_gamma_job(app, client)
    seed_records(app, job_id, [("G-001", "管网压力监测点台账")])
    recon_id = make_recon(app, job_id)

    with app.app_context():
        assert recon.eligible_catchup_codes(recon_id) == [
            "G-002",
            "G-003",
            "G-004",
        ]
        summary = recon.create_catchup(recon_id)

    assert summary["status"] == "done"
    assert summary["inserted"] == 3
    # 补差写的是正式结果表。
    with app.app_context():
        db = get_db()
        skus = [
            json.loads(r["payload_json"])["sku"]
            for r in db.execute(
                "SELECT payload_json FROM crawl_records WHERE job_id = ?", (job_id,)
            ).fetchall()
        ]
    assert set(skus) == {"G-001", "G-002", "G-003", "G-004"}
    # 补差后自动产生新对账批次，且新批次缺失已清零。
    new_grouped = diffs_by_type(app, summary["new_reconciliation_id"])
    assert new_grouped[recon.DIR_ONLY] == []


# --------------------------------------------------------------------------- #
# 4. 补差只含指定编码，不顺手抓目录里的其它编码
# --------------------------------------------------------------------------- #
def test_catchup_contains_only_specified_codes(app, client):
    job_id = create_gamma_job(app, client)
    # 库里只有 G-001、G-003，缺失 G-002、G-004。
    seed_records(
        app,
        job_id,
        [
            ("G-001", "管网压力监测点台账"),
            ("G-003", "雨水口清淤登记"),
        ],
    )
    recon_id = make_recon(app, job_id)

    with app.app_context():
        codes = recon.eligible_catchup_codes(recon_id)
        assert codes == ["G-002", "G-004"]
        summary = recon.create_catchup(recon_id)

    assert summary["codes"] == ["G-002", "G-004"]
    with app.app_context():
        task = get_db().execute(
            "SELECT item_codes_json FROM catchup_tasks WHERE id = ?",
            (summary["catchup_id"],),
        ).fetchone()
    assert json.loads(task["item_codes_json"]) == ["G-002", "G-004"]
    # 正式结果恰好四个目录编码，没有任何名单外编码被抓进来。
    with app.app_context():
        skus = {
            json.loads(r["payload_json"])["sku"]
            for r in get_db().execute(
                "SELECT payload_json FROM crawl_records WHERE job_id = ?", (job_id,)
            ).fetchall()
        }
    assert skus == {"G-001", "G-002", "G-003", "G-004"}


# --------------------------------------------------------------------------- #
# 5. 同一批差异不能生成两次补差
# --------------------------------------------------------------------------- #
def test_same_batch_cannot_catchup_twice(app, client):
    job_id = create_gamma_job(app, client)
    seed_records(app, job_id, [("G-001", "管网压力监测点台账")])
    recon_id = make_recon(app, job_id)

    with app.app_context():
        recon.create_catchup(recon_id)
        with pytest.raises(ValueError, match="已生成过补差"):
            recon.create_catchup(recon_id)

    # 数据库层面也保证一批最多一条补差。
    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) AS c FROM catchup_tasks WHERE reconciliation_id = ?",
            (recon_id,),
        ).fetchone()["c"]
    assert count == 1

    # 路由上再次触发应被拒绝并闪错，而不是再建一条。
    resp = client.post(f"/reconciliation/{recon_id}/catchup", follow_redirects=True)
    assert resp.status_code == 200
    assert "不能重复补差" in resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 6. 标题不同不会被当成缺失进入补差
# --------------------------------------------------------------------------- #
def test_title_mismatch_never_enters_catchup(app, client):
    job_id = create_gamma_job(app, client)
    seed_records(
        app,
        job_id,
        [
            ("G-001", "管网压力监测点台账"),
            ("G-002", "旧版河道标题（已改名）"),  # 标题不同，编码两边都有
            ("G-003", "雨水口清淤登记"),
            ("G-004", "污水厂进水水质报表"),
        ],
    )
    recon_id = make_recon(app, job_id)
    grouped = diffs_by_type(app, recon_id)

    assert [d["item_code"] for d in grouped[recon.TITLE_MISMATCH]] == ["G-002"]
    # 没有任何缺失，因此根本不允许补差。
    with app.app_context():
        assert recon.eligible_catchup_codes(recon_id) == []
        with pytest.raises(ValueError, match="没有可补差的编码"):
            recon.create_catchup(recon_id)

    # 库侧标题保持原样，对账没有改动正式结果。
    with app.app_context():
        row = get_db().execute(
            "SELECT title FROM crawl_records WHERE job_id = ? AND title LIKE ?",
            (job_id, "旧版河道标题%"),
        ).fetchone()
    assert row is not None


# --------------------------------------------------------------------------- #
# 7. 补差正在运行时拒绝再次触发
# --------------------------------------------------------------------------- #
def test_catchup_running_blocks_second_trigger(app, client, monkeypatch):
    job_id = create_gamma_job(app, client)
    seed_records(app, job_id, [("G-001", "管网压力监测点台账")])
    recon_id = make_recon(app, job_id)

    # 让补差停在 running：执行器在写库前抛错，任务记录保持 running。
    def boom(_catchup_id):
        raise RuntimeError("simulated long running catchup")

    monkeypatch.setattr(recon, "_run_catchup", boom)
    with app.app_context():
        with pytest.raises(RuntimeError, match="simulated long running"):
            recon.create_catchup(recon_id)

    with app.app_context():
        state = get_db().execute(
            "SELECT status FROM catchup_tasks WHERE reconciliation_id = ?",
            (recon_id,),
        ).fetchone()["status"]
    assert state == "running"

    # 服务层：运行中再次触发被拒绝。
    with app.app_context():
        with pytest.raises(ValueError, match="正在运行"):
            recon.create_catchup(recon_id)

    # 路由层：同样拒绝，页面提示运行中。
    resp = client.post(f"/reconciliation/{recon_id}/catchup", follow_redirects=True)
    assert "补差正在运行" in resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 附加：任务正常运行结束后会自动生成对账批次
# --------------------------------------------------------------------------- #
def test_run_job_auto_creates_reconciliation(app, client):
    job_id = create_gamma_job(app, client)
    with app.app_context():
        summary = run_job(job_id)
    assert "reconciliation_id" in summary
    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) AS c FROM reconciliations WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
    assert count == 1
    # 全量抓完时三类差异都应为空。
    grouped = diffs_by_type(app, summary["reconciliation_id"])
    assert all(not rows for rows in grouped.values())


# --------------------------------------------------------------------------- #
# 附加：对账页需要登录，且能看出保留状态
# --------------------------------------------------------------------------- #
def test_reconciliation_page_requires_login_and_shows_retention(app, client):
    # 未登录被重定向到登录页。
    resp = client.get("/jobs/1/reconciliation")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    job_id = create_gamma_job(app, client)
    seed_records(app, job_id, [("G-001", "管网压力监测点台账")])
    recon_id = make_recon(app, job_id)
    grouped = diffs_by_type(app, recon_id)
    retain(app, recon_id, grouped[recon.DIR_ONLY][0]["id"], "测试保留原因")

    html = client.get(
        f"/jobs/{job_id}/reconciliation?recon={recon_id}"
    ).get_data(as_text=True)
    assert "已保留" in html
    assert "测试保留原因" in html
    assert "未保留" in html
