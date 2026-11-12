"""对账与补差的页面路由。

差异由服务端 ``app.services.reconciliation`` 计算，本层只负责展示、筛选和
转发人工动作（保留 / 重新对账 / 生成补差），不在这里实现业务规则。
"""

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.db import get_db
from app.routes.auth import login_required
from app.services import reconciliation as recon_service

bp = Blueprint("reconciliation", __name__)

VALID_TYPES = (
    recon_service.DIR_ONLY,
    recon_service.DB_ONLY,
    recon_service.TITLE_MISMATCH,
)


def _load_recon(job_id: int, recon_id: int | None):
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        return None, None, []
    if recon_id is not None:
        recon, diffs = recon_service.get_reconciliation(recon_id)
        if recon is None or recon["job_id"] != job_id:
            return job, None, []
    else:
        recon, diffs = recon_service.latest_reconciliation(job_id)
    return job, recon, diffs


@bp.get("/jobs/<int:job_id>/reconciliation")
@login_required
def view(job_id: int):
    diff_type = request.args.get("type", "").strip()
    if diff_type not in VALID_TYPES:
        diff_type = ""
    selected_recon = request.args.get("recon", type=int)

    job, recon, diffs = _load_recon(job_id, selected_recon)
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))

    db = get_db()
    history = db.execute(
        "SELECT id, created_at FROM reconciliations WHERE job_id = ? ORDER BY id DESC",
        (job_id,),
    ).fetchall()

    grouped: dict[str, list] = {t: [] for t in VALID_TYPES}
    for d in diffs:
        grouped[d["diff_type"]].append(d)

    catchup = None
    eligible_codes: list[str] = []
    if recon is not None:
        catchup = db.execute(
            "SELECT * FROM catchup_tasks WHERE reconciliation_id = ?",
            (recon["id"],),
        ).fetchone()
        eligible_codes = recon_service.eligible_catchup_codes(recon["id"])

    return render_template(
        "reconciliation.html",
        job=job,
        recon=recon,
        history=history,
        grouped=grouped,
        labels=recon_service.DIFF_LABELS,
        diff_type=diff_type,
        catchup=catchup,
        eligible_codes=eligible_codes,
    )


@bp.post("/jobs/<int:job_id>/reconciliation/rerun")
@login_required
def rerun(job_id: int):
    try:
        recon_id = recon_service.create_reconciliation(job_id)
        flash("已按目录当前状态重新对账（正式结果未改动）", "ok")
        return redirect(
            url_for("reconciliation.view", job_id=job_id, recon=recon_id)
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("reconciliation.view", job_id=job_id))


@bp.post("/reconciliation/<int:recon_id>/retain/<int:diff_id>")
@login_required
def retain(recon_id: int, diff_id: int):
    db = get_db()
    recon = db.execute(
        "SELECT job_id FROM reconciliations WHERE id = ?", (recon_id,)
    ).fetchone()
    if recon is None:
        flash("对账批次不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    try:
        recon_service.retain_diff(recon_id, diff_id, request.form.get("reason", ""))
        flash("已标记为人工保留", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(
        url_for(
            "reconciliation.view",
            job_id=recon["job_id"],
            recon=recon_id,
            type=request.form.get("filter_type", ""),
        )
    )


@bp.post("/reconciliation/<int:recon_id>/catchup")
@login_required
def catchup(recon_id: int):
    db = get_db()
    recon = db.execute(
        "SELECT job_id FROM reconciliations WHERE id = ?", (recon_id,)
    ).fetchone()
    if recon is None:
        flash("对账批次不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    try:
        summary = recon_service.create_catchup(recon_id)
        flash(
            "补差完成：抓取编码 {} 个，写入 {} 条，去重跳过 {} 条".format(
                len(summary["codes"]),
                summary["inserted"],
                summary["skipped_dup"],
            ),
            "ok",
        )
        # 补差后已自动生成新批次，跳到最新批次。
        return redirect(
            url_for(
                "reconciliation.view",
                job_id=recon["job_id"],
                recon=summary["new_reconciliation_id"],
            )
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(
            url_for("reconciliation.view", job_id=recon["job_id"], recon=recon_id)
        )
