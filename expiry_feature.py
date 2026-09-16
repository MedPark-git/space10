"""Additive expiry workflow. Existing item and movement tables are never written."""
import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from expiry_engine import FORMATS, InputError, calculate, family_rules, index_mts, iso_date, number, parse_family_rule, parse_paste, stock_scope, summarize


def make_models(db):
    class Reference(db.Model):
        __tablename__ = "expiry_references"
        id = db.Column(db.Integer, primary_key=True)
        kind = db.Column(db.String(30), nullable=False)
        key = db.Column(db.String(1100), nullable=False)
        payload = db.Column(db.JSON, nullable=False)
        updated_by = db.Column(db.String(36), nullable=False)
        updated_at = db.Column(db.DateTime(timezone=True), nullable=False)
        __table_args__ = (db.UniqueConstraint("kind", "key", name="uq_expiry_reference"),)

    class Import(db.Model):
        __tablename__ = "expiry_imports"
        id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
        kind = db.Column(db.String(30), nullable=False)
        payload = db.Column(db.JSON, nullable=False)
        notes = db.Column(db.JSON, nullable=False)
        base_revision = db.Column(db.String(64), nullable=False)
        as_of = db.Column(db.Date, nullable=True)
        created_by = db.Column(db.String(36), nullable=False, index=True)
        created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
        committed_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    return Reference, Import


def register_expiry(app, db, audit, roles):
    if not hasattr(db, "_expiry_models"):
        db._expiry_models = make_models(db)
    Reference, Import = db._expiry_models
    bp = Blueprint("expiry", __name__, url_prefix="/expiry")

    def references():
        result = {"family_rules": family_rules({})}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def revision(refs):
        return hashlib.sha256(json.dumps(refs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def lock_writes():
        # Serializes reference/snapshot commits across Gunicorn workers.
        db.session.execute(text("SELECT pg_advisory_xact_lock(73190422)"))

    def current_snapshot():
        snapshot_id = request.args.get("snapshot", "")
        stmt = select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None))
        if snapshot_id:
            return db.session.scalar(stmt.where(Import.id == snapshot_id))
        return db.session.scalar(stmt.order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))

    def latest_snapshot():
        return db.session.scalar(select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None))
                                 .order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))

    def rule_impact(proposed, refs, snapshot, as_of):
        products, scope = stock_scope(snapshot.payload if snapshot else [])
        before_refs = index_mts(refs)
        after_refs = index_mts({**refs, "family_rules": {**refs["family_rules"], proposed["prefix"]: proposed}})
        impact = {"targets": 0, "date_changes": 0, "unresolved_before": 0, "unresolved_after": 0,
                  "unknown_accounts": scope["계정구분 미확인 제외"], "unmapped_products": 0, "examples": []}
        for entry in products:
            row = entry["data"]
            mapping = refs.get("mapping", {}).get(row["erp"], {})
            if number(row["quantity"]) != 0 and (not mapping or mapping.get("icube") in {"미관리", "미관", "N/A", "#N/A"}):
                impact["unmapped_products"] += 1
            if not mapping.get("icube", "").startswith(proposed["prefix"]) or number(row["quantity"]) == 0:
                continue
            before = calculate(row, before_refs, as_of)
            after = calculate(row, after_refs, as_of)
            impact["targets"] += 1
            impact["date_changes"] += before["expiry"] != after["expiry"]
            impact["unresolved_before"] += bool(before["error"])
            impact["unresolved_after"] += bool(after["error"])
            if len(impact["examples"]) < 30:
                impact["examples"].append({"name": row["name"], "icube": mapping["icube"], "lot": row["lot"],
                                            "before": before["expiry"] or before["error"],
                                            "after": after["expiry"] or after["error"], "source": after["source"]})
        return impact

    @bp.get("/special-rules")
    @login_required
    def special_rules():
        refs = references()
        edit = request.args.get("edit", "").strip().upper()
        if edit and edit not in refs["family_rules"]:
            abort(404)
        values = refs["family_rules"].get(edit, {"prefix": "", "days": 1095, "enabled": True})
        history = db.session.scalars(select(Import).where(Import.kind == "family_rules", Import.committed_at.is_not(None))
                                    .order_by(Import.committed_at.desc()).limit(20)).all()
        return render_template("expiry_special_rules.html", rules=refs["family_rules"], values=values,
                               edit=edit, revision=revision(refs), history=history)

    @bp.post("/special-rules/preview")
    @roles("admin", "editor")
    def special_rule_preview_create():
        try:
            proposed = parse_family_rule(request.form)
            refs = references()
            if request.form.get("revision") != revision(refs):
                raise InputError("화면을 연 뒤 기준정보가 바뀌었습니다. 새로 열린 화면에서 변경 내용을 다시 입력해 주세요.")
            snapshot = latest_snapshot()
            as_of = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date()
            impact = rule_impact(proposed, refs, snapshot, as_of)
            pending = Import(kind="family_rules", payload=[{"key": proposed["prefix"], "data": proposed}],
                             notes={"before": refs["family_rules"].get(proposed["prefix"]), "impact": impact,
                                    "snapshot_id": snapshot.id if snapshot else None,
                                    "snapshot_date": snapshot.as_of.isoformat() if snapshot else None,
                                    "actor_name": current_user.name},
                             base_revision=revision(refs), as_of=as_of, created_by=current_user.id)
            db.session.add(pending)
            db.session.commit()
            return redirect(url_for("expiry.special_rule_preview", import_id=pending.id))
        except InputError as exc:
            flash(str(exc), "error")
            return redirect(url_for("expiry.special_rules"))

    @bp.get("/special-rules/preview/<import_id>")
    @roles("admin", "editor")
    def special_rule_preview(import_id):
        pending = db.get_or_404(Import, import_id)
        if pending.kind != "family_rules":
            abort(404)
        if pending.created_by != current_user.id:
            abort(403)
        snapshot = latest_snapshot()
        stale = pending.base_revision != revision(references()) or pending.notes["snapshot_id"] != (snapshot.id if snapshot else None)
        return render_template("expiry_special_preview.html", pending=pending, proposed=pending.payload[0]["data"],
                               before=pending.notes["before"], impact=pending.notes["impact"], stale=stale)

    def current_view():
        refs = references()
        snapshot = current_snapshot()
        try:
            as_of = iso_date(request.args.get("as_of") or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Seoul')).date().isoformat())
        except InputError:
            abort(400)
        thresholds = tuple(refs.get("settings", {}).get("alerts", {}).get("days", [90, 180, 365]))
        indexed = index_mts(refs)
        products, scope = stock_scope(snapshot.payload if snapshot else [])
        rows = [calculate(e["data"], indexed, as_of, thresholds) for e in products]
        summary = summarize(rows)
        summary["scope"] = scope
        warehouses = sorted({r["warehouse"] for r in rows})
        statuses = sorted({r["status"] for r in rows})
        q = request.args.get("q", "").strip().casefold()
        filtered = [r for r in rows if
                    (request.args.get("zero") == "1" or number(r["quantity"]) != 0)
                    and (not request.args.get("warehouse") or r["warehouse"] == request.args["warehouse"])
                    and (not request.args.get("factory") or r["factory"] == request.args["factory"])
                    and (not request.args.get("status") or r["status"] == request.args["status"])
                    and (not q or any(q in str(r.get(k, "")).casefold() for k in ("erp", "icube", "name", "lot", "location")))]
        filtered.sort(key=lambda r: (0 if r["error"] else 1, r["expiry"] or "", r["name"], r["lot"]))
        return refs, snapshot, as_of, summary, warehouses, statuses, filtered

    @bp.get("/")
    @login_required
    def index():
        refs, snapshot, as_of, summary, warehouses, statuses, rows = current_view()
        page = max(1, request.args.get("page", 1, type=int))
        pages = max(1, (len(rows) + 99) // 100)
        page = min(page, pages)
        args = request.args.to_dict()
        args.pop("page", None)
        history = db.session.scalars(select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None)).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(100)).all()
        return render_template("expiry.html", rows=rows[(page-1)*100:page*100], total=len(rows), page=page, pages=pages,
                               previous_url=url_for("expiry.index", **args, page=page-1), next_url=url_for("expiry.index", **args, page=page+1),
                               export_url=url_for("expiry.export", **args), history=history, snapshot=snapshot,
                               as_of=as_of, summary=summary, warehouses=warehouses, statuses=statuses,
                               ref_counts={k: len(v) for k, v in refs.items()}, formats=FORMATS)

    @bp.route("/import/<kind>", methods=["GET", "POST"])
    @roles("admin", "editor")
    def import_data(kind):
        if kind not in FORMATS:
            abort(404)
        if request.method == "POST":
            try:
                entries, notes = parse_paste(kind, request.form.get("data", ""))
                as_of = iso_date(request.form.get("as_of")) if kind == "stock" else None
                refs = references()
                old = refs.get(kind, {})
                notes.update({"신규": sum(e["key"] not in old for e in entries),
                              "변경": sum(e["key"] in old and old[e["key"]] != e["data"] for e in entries),
                              "동일": sum(old.get(e["key"]) == e["data"] for e in entries)})
                if kind == "rules":
                    merged = {**old, **{e["key"]: e["data"] for e in entries}}
                    for rule in merged.values():
                        if rule["factory"] == "1·2" and rule["prefix"] + "|*" in merged:
                            raise InputError(f"{rule['prefix']}: 1·2공장과 3공장 규칙이 충돌합니다.")
                pending = Import(kind=kind, payload=entries, notes=notes, as_of=as_of,
                                 base_revision=revision(refs), created_by=current_user.id)
                db.session.add(pending)
                db.session.commit()
                return redirect(url_for("expiry.preview", import_id=pending.id))
            except InputError as exc:
                flash(str(exc), "error")
        return render_template("expiry_import.html", kind=kind, title=FORMATS[kind][0], header=FORMATS[kind][1], formats=FORMATS)

    @bp.get("/preview/<import_id>")
    @roles("admin", "editor")
    def preview(import_id):
        pending = db.get_or_404(Import, import_id)
        if pending.created_by != current_user.id:
            abort(403)
        if pending.kind == "family_rules":
            return redirect(url_for("expiry.special_rule_preview", import_id=pending.id))
        refs = references()
        entries, scope = stock_scope(pending.payload) if pending.kind == "stock" else (pending.payload, {})
        changes = [{"key": e["key"], "before": refs.get(pending.kind, {}).get(e["key"]), "after": e["data"]} for e in entries]
        indexed = index_mts(refs)
        examples = [calculate(e["data"], indexed, pending.as_of) for e in entries] if pending.kind == "stock" else []
        page = max(1, request.args.get('page', 1, type=int))
        pages = max(1, (len(changes)+99)//100)
        page = min(page, pages)
        return render_template("expiry_preview.html", pending=pending, title=FORMATS[pending.kind][0], changes=changes[(page-1)*100:page*100], page=page, pages=pages,
                               total=len(changes), summary=summarize(examples), scope=scope, examples=examples[:30],
                               quantity=str(sum((number(e["data"]["quantity"]) for e in pending.payload), number(0))) if pending.kind == "stock" else None)

    @bp.post("/commit/<import_id>")
    @roles("admin", "editor")
    def commit(import_id):
        lock_writes()
        pending = db.session.scalar(select(Import).where(Import.id == import_id).with_for_update())
        if not pending:
            abort(404)
        if pending.created_by != current_user.id:
            abort(403)
        if pending.committed_at:
            flash("이미 등록된 자료입니다. 중복 등록하지 않았습니다.", "success")
            return redirect(url_for("expiry.special_rules" if pending.kind == "family_rules" else "expiry.index"))
        if pending.base_revision != revision(references()):
            if pending.kind == "family_rules":
                flash("미리보기 이후 기준정보가 변경되었습니다. 특이사항 관리에서 적용 영향을 다시 확인해 주세요.", "error")
                return redirect(url_for("expiry.special_rules"))
            flash("미리보기 이후 기준정보가 변경되었습니다. 자료를 다시 붙여넣어 확인해 주세요.", "error")
            return redirect(url_for("expiry.import_data", kind=pending.kind))
        if pending.kind == "family_rules":
            snapshot = latest_snapshot()
            if pending.notes["snapshot_id"] != (snapshot.id if snapshot else None):
                flash("미리보기 이후 최신 재고가 바뀌었습니다. 적용 영향을 다시 확인해 주세요.", "error")
                return redirect(url_for("expiry.special_rules"))
        if pending.kind == "stock" and any("account" not in e["data"] for e in pending.payload):
            flash("계정구분이 없는 이전 미리보기입니다. 계정구분 열을 포함해 재고를 다시 붙여넣어 주세요.", "error")
            return redirect(url_for("expiry.import_data", kind="stock"))
        now = datetime.now(timezone.utc)
        if pending.kind != "stock":
            existing = {r.key: r for r in db.session.scalars(select(Reference).where(Reference.kind == pending.kind))}
            for entry in pending.payload:
                row = existing.get(entry["key"])
                if row is None:
                    row = Reference(kind=pending.kind, key=entry["key"])
                    db.session.add(row)
                row.payload = entry["data"]
                row.updated_by = current_user.id
                row.updated_at = now
        pending.committed_at = now
        if pending.kind == "family_rules":
            audit("expiry_special_rule_updated", target_type="expiry_import", target_id=pending.id,
                  detail=json.dumps({"before": pending.notes["before"], "after": pending.payload[0]["data"],
                                     "snapshot_id": pending.notes["snapshot_id"],
                                     "date_changes": pending.notes["impact"]["date_changes"]}, ensure_ascii=False), commit=False)
        else:
            audit("expiry_import_committed", target_type="expiry_import", target_id=pending.id,
                  detail=f"{pending.kind}:{len(pending.payload)}", commit=False)
        db.session.commit()
        if pending.kind == "family_rules":
            flash("품번 계열 공통 규칙을 반영했습니다. 변경 이력에서 이전 기준을 확인할 수 있습니다.", "success")
            return redirect(url_for("expiry.special_rules"))
        flash(f"{FORMATS[pending.kind][0]} {len(pending.payload):,}건이 등록되었습니다.", "success")
        return redirect(url_for("expiry.index", **({"snapshot": pending.id} if pending.kind == "stock" else {})))

    @bp.get("/references/<kind>")
    @login_required
    def reference_list(kind):
        if kind not in FORMATS or kind == "stock":
            abort(404)
        values = references().get(kind, {})
        q = request.args.get("q", "").strip().casefold()
        rows = [v for k, v in values.items() if not q or q in (k + json.dumps(v, ensure_ascii=False)).casefold()]
        page = max(1, request.args.get("page", 1, type=int))
        pages = max(1, (len(rows)+99)//100)
        page = min(page, pages)
        return render_template("expiry_references.html", kind=kind, title=FORMATS[kind][0], rows=rows[(page-1)*100:page*100],
                               total=len(rows), page=page, pages=pages, q=q, formats=FORMATS)

    @bp.route("/alerts", methods=["GET", "POST"])
    @roles("admin", "editor")
    def alerts():
        if request.method == "POST":
            try:
                days = [int(request.form.get(f"day{i}", "")) for i in range(1, 4)]
                if not 1 <= days[0] < days[1] < days[2] <= 3650:
                    raise ValueError()
                lock_writes()
                row = db.session.scalar(select(Reference).where(Reference.kind == "settings", Reference.key == "alerts"))
                if row is None:
                    row = Reference(kind="settings", key="alerts")
                    db.session.add(row)
                row.payload = {"days": days}
                row.updated_by = current_user.id
                row.updated_at = datetime.now(timezone.utc)
                audit("expiry_alerts_updated", detail=json.dumps(days), commit=False)
                db.session.commit()
                flash("화면 알림 기준이 변경되었습니다.", "success")
                return redirect(url_for("expiry.index"))
            except (ValueError, TypeError):
                flash("알림 일수는 1~3,650일 범위에서 작은 순서로 입력해 주세요.", "error")
        days = references().get("settings", {}).get("alerts", {}).get("days", [90, 180, 365])
        return render_template("expiry_alerts.html", days=days)

    @bp.get("/export.csv")
    @login_required
    def export():
        _, _, _, _, _, _, rows = current_view()
        columns = [("warehouse", "창고"), ("location", "장소"), ("erp", "아마란스 품번"), ("icube", "ICUBE 품번"),
                   ("name", "품명"), ("spec", "규격"), ("account", "계정구분"), ("lot", "LOT"), ("unit", "단위"), ("quantity", "기말재고"),
                   ("factory", "공장"), ("expiry", "사용기한"), ("remaining", "잔여일"), ("status", "상태"), ("error", "확인사항"), ("source", "계산근거")]
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow([v for k, v in columns])
        for row in rows:
            cells = []
            for key, _ in columns:
                value = "" if row.get(key) is None else str(row[key])
                if key not in {"quantity", "remaining"} and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
                    value = "'" + value
                cells.append(value)
            writer.writerow(cells)
        return Response("\ufeff" + stream.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=inventory_expiry.csv"})

    app.register_blueprint(bp)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.config["MAX_FORM_MEMORY_SIZE"] = 10 * 1024 * 1024
    app.jinja_env.filters["qty"] = lambda value: format(number(value), ",f").rstrip("0").rstrip(".") if "." in format(number(value), ",f") else format(number(value), ",f")
    app.extensions["expiry_models"] = {"Reference": Reference, "Import": Import}
