"""Additive expiry workflow. Existing item and movement tables are never written."""
import csv
import hashlib
import io
import json
import re
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
        bucket = request.args.get("bucket", "")
        def in_bucket(row):
            quantity = number(row["quantity"])
            unit = (row.get("unit") or "").strip().upper()
            if bucket == "all_ea":
                return unit == "EA" and quantity > 0
            if bucket == "negative":
                return quantity < 0
            if bucket == "non_ea":
                return quantity > 0 and unit != "EA"
            if bucket == "expired":
                return unit == "EA" and quantity > 0 and not row["error"] and row["remaining"] is not None and row["remaining"] <= 0
            if bucket == "due90":
                return unit == "EA" and quantity > 0 and not row["error"] and row["remaining"] is not None and 0 < row["remaining"] <= 90
            if bucket == "due180":
                return unit == "EA" and quantity > 0 and not row["error"] and row["remaining"] is not None and 90 < row["remaining"] <= 180
            if bucket == "due365":
                return unit == "EA" and quantity > 0 and not row["error"] and row["remaining"] is not None and 180 < row["remaining"] <= 365
            if bucket == "safe":
                return unit == "EA" and quantity > 0 and not row["error"] and row["remaining"] is not None and row["remaining"] > 365
            if bucket == "issue":
                return unit == "EA" and quantity > 0 and bool(row["error"])
            return True
        filtered = [r for r in rows if
                    (request.args.get("zero") == "1" or number(r["quantity"]) != 0)
                    and in_bucket(r)
                    and (not request.args.get("warehouse") or r["warehouse"] == request.args["warehouse"])
                    and (not request.args.get("location") or (r.get("location") or "") == request.args["location"])
                    and (not request.args.get("factory") or r["factory"] == request.args["factory"])
                    and (not request.args.get("status") or r["status"] == request.args["status"])
                    and (not q or any(q in str(r.get(k, "")).casefold() for k in ("erp", "icube", "name", "spec", "lot", "warehouse", "location")))]
        filtered.sort(key=lambda r: (0 if r["error"] else 1, r["expiry"] or "", r["name"], r["lot"]))
        return refs, snapshot, as_of, summary, warehouses, statuses, filtered

    def action_bucket(row):
        if row["error"]:
            return "확인 필요"
        remaining = row["remaining"]
        if remaining is None:
            return "확인 필요"
        if remaining <= 0:
            return "만료"
        if remaining <= 90:
            return "1~90일"
        if remaining <= 180:
            return "91~180일"
        if remaining <= 365:
            return "181~365일"
        return "365일 초과"

    def active_rows(rows):
        return [row for row in rows if number(row["quantity"]) != 0]

    bucket_definitions = [
        ("expired", "만료"), ("due90", "1~90일"), ("due180", "91~180일"),
        ("due365", "181~365일"), ("safe", "365일 초과"), ("issue", "확인 필요"),
    ]

    def is_positive_ea(row):
        return (row.get("unit") or "").strip().upper() == "EA" and number(row["quantity"]) > 0

    def distinct_lots(rows):
        keys = set()
        for index, row in enumerate(rows):
            lot = (row.get("lot") or "").strip()
            key = (row.get("erp"), lot) if lot else (row.get("erp"), "LOT 없음", row.get("warehouse"), row.get("location"), index)
            keys.add(key)
        return len(keys)

    def inventory_stat(rows):
        ea_rows = [row for row in rows if is_positive_ea(row)]
        return {"quantity": sum((number(row["quantity"]) for row in ea_rows), number(0)),
                "lots": distinct_lots(ea_rows), "rows": len(ea_rows),
                "products": len({row["erp"] for row in ea_rows})}

    def inventory_buckets(rows):
        result = []
        for key, label in bucket_definitions:
            group = [row for row in rows if action_bucket(row) == label]
            result.append({"key": key, "label": label, **inventory_stat(group)})
        maximum = max([entry["quantity"] for entry in result] + [number(1)])
        for entry in result:
            entry["percent"] = float(entry["quantity"] * 100 / maximum) if maximum else 0
        return result

    def data_quality_stats(rows):
        negative = [row for row in rows if number(row["quantity"]) < 0]
        non_ea = [row for row in rows if number(row["quantity"]) > 0 and (row.get("unit") or "").strip().upper() != "EA"]
        by_unit = []
        for unit in sorted({(row.get("unit") or "").strip().upper() or "단위 미확인" for row in non_ea}):
            group = [row for row in non_ea if ((row.get("unit") or "").strip().upper() or "단위 미확인") == unit]
            by_unit.append({"unit": unit, "quantity": sum((number(row["quantity"]) for row in group), number(0)),
                            "lots": distinct_lots(group)})
        return {"negative": {"quantity": abs(sum((number(row["quantity"]) for row in negative), number(0))),
                              "lots": distinct_lots(negative)},
                "non_ea": by_unit, "non_ea_lots": distinct_lots(non_ea)}

    default_available_warehouse_keys = {"완제품창고", "3공장완제품창고"}

    def warehouse_key(name):
        return re.sub(r"\s+", "", str(name or "")).casefold()

    def warehouse_classification(name, refs):
        key = warehouse_key(name)
        custom = refs.get("warehouse_classes", {}).get(key)
        if custom and custom.get("classification") in {"available", "unavailable"}:
            return custom["classification"], "관리자 설정"
        if key in default_available_warehouse_keys:
            return "available", "기본 가용창고"
        return "unclassified", "분류 필요"

    def availability_stats(rows, refs):
        result = {}
        for classification in ("available", "unavailable", "unclassified"):
            group = [row for row in rows if warehouse_classification(row.get("warehouse"), refs)[0] == classification]
            result[classification] = inventory_stat(group)
        return result

    def product_risk_groups(rows, refs, limit=12):
        groups = {}
        for row in rows:
            if not is_positive_ea(row):
                continue
            key = (row["erp"], row["name"], row.get("spec") or "규격 미등록")
            group = groups.setdefault(key, {"erp": row["erp"], "icube": row.get("icube"), "name": row["name"],
                                             "spec": row.get("spec") or "규격 미등록", "total": number(0),
                                             "expired": number(0), "due90": number(0), "issues": number(0),
                                             "lot_keys": set(), "warehouses": set(), "locations": set(), "nearest": None})
            quantity = number(row["quantity"])
            group["total"] += quantity
            bucket = action_bucket(row)
            if bucket == "만료":
                group["expired"] += quantity
            elif bucket == "1~90일":
                group["due90"] += quantity
            elif bucket == "확인 필요":
                group["issues"] += quantity
            group["lot_keys"].add((row["erp"], row["lot"] or (row["warehouse"], row["location"])))
            group["warehouses"].add(row["warehouse"] or "미지정")
            group["locations"].add((row["warehouse"] or "미지정", row["location"] or "장소 미지정"))
            if row["remaining"] is not None:
                group["nearest"] = row["remaining"] if group["nearest"] is None else min(group["nearest"], row["remaining"])
        results = []
        for group in groups.values():
            if not (group["expired"] or group["due90"] or group["issues"]):
                continue
            group["lots"] = len(group.pop("lot_keys"))
            group["warehouses"] = sorted(group["warehouses"])
            group["locations"] = sorted(group["locations"])
            results.append(group)
        results.sort(key=lambda group: (group["expired"], group["due90"], group["issues"], group["total"]), reverse=True)
        return results[:limit]

    def risk_location_groups(rows, refs, bucket_key, limit=12):
        """Break a headline risk quantity into product/spec and physical stock locations."""
        bucket_labels = dict(bucket_definitions)
        label = bucket_labels[bucket_key]
        groups = {}
        for row in rows:
            if not is_positive_ea(row) or action_bucket(row) != label:
                continue
            warehouse = row.get("warehouse") or "미지정"
            location = row.get("location") or "장소 미지정"
            spec = row.get("spec") or "규격 미등록"
            key = (row["erp"], row["name"], spec, warehouse, location)
            classification, _ = warehouse_classification(warehouse, refs)
            group = groups.setdefault(key, {
                "erp": row["erp"], "icube": row.get("icube"), "name": row["name"], "spec": spec,
                "warehouse": warehouse, "location": location, "classification": classification,
                "quantity": number(0), "lot_keys": set(), "nearest": None, "nearest_expiry": None,
                "error": None,
            })
            group["quantity"] += number(row["quantity"])
            group["lot_keys"].add((row["erp"], row.get("lot") or (warehouse, location)))
            if row.get("remaining") is not None and (group["nearest"] is None or row["remaining"] < group["nearest"]):
                group["nearest"] = row["remaining"]
                group["nearest_expiry"] = row.get("expiry")
            if row.get("error") and not group["error"]:
                group["error"] = row["error"]
        results = []
        for group in groups.values():
            group["lots"] = len(group.pop("lot_keys"))
            results.append(group)
        results.sort(key=lambda group: (group["quantity"], group["lots"]), reverse=True)
        return results[:limit]

    def inventory_location_groups(rows, refs, limit=20):
        """Show actual stock as one row per product/spec/warehouse/location/unit."""
        groups = {}
        for row in rows:
            if number(row["quantity"]) <= 0:
                continue
            warehouse = row.get("warehouse") or "미지정"
            location = row.get("location") or "장소 미지정"
            spec = row.get("spec") or "규격 미등록"
            unit = (row.get("unit") or "단위 미확인").strip() or "단위 미확인"
            key = (warehouse, location, row["erp"], row["name"], spec, unit)
            classification, _ = warehouse_classification(warehouse, refs)
            group = groups.setdefault(key, {
                "erp": row["erp"], "icube": row.get("icube"), "name": row["name"], "spec": spec,
                "warehouse": warehouse, "location": location, "unit": unit,
                "classification": classification, "quantity": number(0), "lot_keys": set(),
            })
            group["quantity"] += number(row["quantity"])
            group["lot_keys"].add((row["erp"], row.get("lot") or (warehouse, location)))
        results = []
        order = {"available": 0, "unavailable": 1, "unclassified": 2}
        for group in groups.values():
            group["lots"] = len(group.pop("lot_keys"))
            results.append(group)
        results.sort(key=lambda group: (order[group["classification"]], group["warehouse"],
                                        -float(group["quantity"]), group["name"], group["spec"]))
        return results[:limit]

    def inventory_product_groups(rows, refs):
        groups = {}
        for row in rows:
            if number(row["quantity"]) <= 0:
                continue
            unit = (row.get("unit") or "단위 미확인").strip() or "단위 미확인"
            key = (row["erp"], row["name"], row.get("spec") or "규격 미등록", unit)
            group = groups.setdefault(key, {"erp": row["erp"], "icube": row.get("icube"), "name": row["name"],
                                             "spec": row.get("spec") or "규격 미등록", "unit": unit,
                                             "total": number(0), "available": number(0), "unavailable": number(0),
                                             "unclassified": number(0), "lot_keys": set(), "places": set()})
            quantity = number(row["quantity"])
            classification, _ = warehouse_classification(row.get("warehouse"), refs)
            group["total"] += quantity
            group[classification] += quantity
            group["lot_keys"].add((row["erp"], row["lot"] or (row["warehouse"], row["location"])))
            group["places"].add((row["warehouse"] or "미지정", row["location"] or "장소 미지정"))
        results = []
        for group in groups.values():
            group["lots"] = len(group.pop("lot_keys"))
            group["places"] = sorted(group["places"])
            results.append(group)
        return sorted(results, key=lambda group: (group["available"], group["total"], group["name"]), reverse=True)

    @bp.get("/dashboard")
    @login_required
    def dashboard():
        refs, snapshot, as_of, summary, _, _, rows = current_view()
        active = active_rows(rows)
        buckets = inventory_buckets(active)
        bucket_map = {entry["key"]: entry for entry in buckets}
        total_stats = inventory_stat(active)
        quality = data_quality_stats(active)
        availability = availability_stats(active, refs)
        expired_breakdown = risk_location_groups(active, refs, "expired")
        due90_breakdown = risk_location_groups(active, refs, "due90")
        inventory_locations = inventory_location_groups(active, refs)
        availability_labels = {"available": "가용재고", "unavailable": "비가용재고", "unclassified": "분류 필요"}
        for row in active:
            row["availability"], row["availability_source"] = warehouse_classification(row.get("warehouse"), refs)
            row["availability_label"] = availability_labels[row["availability"]]
        priority = sorted((row for row in active if is_positive_ea(row) and (row["error"] or (row["remaining"] is not None and row["remaining"] <= 90))),
                          key=lambda row: (0 if row["remaining"] is not None and row["remaining"] <= 0 else
                                           1 if row["remaining"] is not None and row["remaining"] <= 90 else 2,
                                           row["remaining"] if row["remaining"] is not None else 999999,
                                           row["name"], row["lot"]))[:15]
        warehouse_risk = []
        ea_rows = [row for row in active if is_positive_ea(row)]
        for warehouse in sorted({row["warehouse"] or "미지정" for row in ea_rows}):
            group = [row for row in ea_rows if (row["warehouse"] or "미지정") == warehouse]
            warehouse_risk.append({"name": warehouse, "total": inventory_stat(group),
                                   "expired": inventory_stat([row for row in group if action_bucket(row) == "만료"]),
                                   "due90": inventory_stat([row for row in group if action_bucket(row) == "1~90일"]),
                                   "issues": inventory_stat([row for row in group if action_bucket(row) == "확인 필요"])})
        warehouse_risk.sort(key=lambda row: (row["expired"]["quantity"] + row["due90"]["quantity"] + row["issues"]["quantity"],
                                                    row["total"]["quantity"]), reverse=True)
        return render_template("expiry_dashboard.html", snapshot=snapshot, as_of=as_of, summary=summary,
                               metrics={"expired": bucket_map["expired"], "due90": bucket_map["due90"],
                                        "unresolved": bucket_map["issue"], "total": total_stats},
                               buckets=buckets, priority=priority, quality=quality, warehouse_risk=warehouse_risk[:8],
                               availability=availability, expired_breakdown=expired_breakdown,
                               due90_breakdown=due90_breakdown, inventory_locations=inventory_locations,
                               ref_counts={kind: len(refs.get(kind, {})) for kind in FORMATS if kind != "stock"})


    @bp.get("/dashboard-v2")
    @login_required
    def dashboard_v2():
        refs, snapshot, as_of, summary, warehouses, _, rows = current_view()
        positive_ea = [row for row in rows if is_positive_ea(row)]

        availability_labels = {"available": "가용재고", "unavailable": "불용재고", "unclassified": "분류 필요"}
        selected_availability = request.args.get("availability", "").strip()
        if selected_availability not in availability_labels:
            selected_availability = ""

        for row in positive_ea:
            row["availability"], row["availability_source"] = warehouse_classification(row.get("warehouse"), refs)
            row["availability_label"] = availability_labels[row["availability"]]

        filtered = [row for row in positive_ea if not selected_availability or row["availability"] == selected_availability]

        product_groups = inventory_product_groups(filtered, refs)
        warehouse_names = sorted({row.get("warehouse") or "미지정" for row in filtered})
        matrix = {}
        for row in filtered:
            key = (row["erp"], row["name"], row.get("spec") or "규격 미등록")
            group = matrix.setdefault(key, {
                "erp": row["erp"], "icube": row.get("icube"), "name": row["name"],
                "spec": row.get("spec") or "규격 미등록",
                "total": number(0), "available": number(0), "unavailable": number(0),
                "unclassified": number(0), "by_warehouse": {}
            })
            qty = number(row["quantity"])
            warehouse = row.get("warehouse") or "미지정"
            group["total"] += qty
            group[row["availability"]] += qty
            group["by_warehouse"][warehouse] = group["by_warehouse"].get(warehouse, number(0)) + qty
        products = sorted(matrix.values(), key=lambda g: (g["total"], g["name"]), reverse=True)

        total_qty = sum((number(row["quantity"]) for row in filtered), number(0))
        availability = availability_stats(filtered, refs)

        colors = {
            "365일 초과": "#17689a",
            "181~365일": "#62bdd6",
            "91~180일": "#62cdb4",
            "1~90일": "#f2c85c",
            "만료": "#ef7777",
            "확인 필요": "#9b8ac4",
        }
        expiry_order = ["365일 초과", "181~365일", "91~180일", "1~90일", "만료", "확인 필요"]
        expiry_totals = {label: number(0) for label in expiry_order}
        expiry_product_map = {}
        for row in filtered:
            label = action_bucket(row)
            expiry_totals[label] += number(row["quantity"])
            key = (row["erp"], row["name"], row.get("spec") or "규격 미등록")
            group = expiry_product_map.setdefault(key, {
                "erp": row["erp"], "name": row["name"], "spec": row.get("spec") or "규격 미등록",
                "total": number(0), "bands": {band: number(0) for band in expiry_order}
            })
            qty = number(row["quantity"])
            group["total"] += qty
            group["bands"][label] += qty

        expiry_products = []
        for group in expiry_product_map.values():
            group["segments"] = [{
                "label": band, "color": colors[band], "quantity": group["bands"][band],
                "percent": float(group["bands"][band] * 100 / group["total"]) if group["total"] else 0
            } for band in expiry_order]
            group["risk"] = group["bands"]["만료"] + group["bands"]["1~90일"] + group["bands"]["91~180일"] + group["bands"]["181~365일"] + group["bands"]["확인 필요"]
            expiry_products.append(group)
        expiry_products.sort(key=lambda g: (g["risk"], g["total"], g["name"]), reverse=True)

        expiry_total = sum(expiry_totals.values(), number(0))
        expiry_composition = []
        gradient = []
        cursor = 0.0
        for band in expiry_order:
            qty = expiry_totals[band]
            percent = float(qty * 100 / expiry_total) if expiry_total else 0.0
            start = cursor
            cursor += percent
            expiry_composition.append({"label": band, "color": colors[band], "quantity": qty, "percent": percent})
            if percent > 0:
                gradient.append(f"{colors[band]} {start:.3f}% {cursor:.3f}%")
        expiry_gradient = "conic-gradient(" + ",".join(gradient) + ")" if gradient else "#e8eef3"

        top_products = products[:4]
        comp_colors = ["#17689a", "#3f9fd0", "#65c3d0", "#91d2c6", "#f0aaa6"]
        comp_total = sum((g["total"] for g in products), number(0))
        product_composition = []
        used = number(0)
        for idx, group in enumerate(top_products):
            used += group["total"]
            product_composition.append({
                "name": group["name"], "spec": group["spec"], "quantity": group["total"],
                "percent": float(group["total"] * 100 / comp_total) if comp_total else 0,
                "color": comp_colors[idx]
            })
        if comp_total - used > 0:
            product_composition.append({
                "name": "기타", "spec": "", "quantity": comp_total - used,
                "percent": float((comp_total-used) * 100 / comp_total) if comp_total else 0,
                "color": comp_colors[4]
            })
        comp_gradient = []
        cursor = 0.0
        for item in product_composition:
            start = cursor
            cursor += item["percent"]
            comp_gradient.append(f"{item['color']} {start:.3f}% {cursor:.3f}%")
        product_gradient = "conic-gradient(" + ",".join(comp_gradient) + ")" if comp_gradient else "#e8eef3"

        priority = sorted(
            (row for row in filtered if row["error"] or (row["remaining"] is not None and row["remaining"] <= 365)),
            key=lambda row: (
                0 if row["remaining"] is not None and row["remaining"] <= 0 else
                1 if row["remaining"] is not None and row["remaining"] <= 90 else
                2 if row["remaining"] is not None and row["remaining"] <= 180 else
                3 if row["remaining"] is not None and row["remaining"] <= 365 else 4,
                row["remaining"] if row["remaining"] is not None else 999999,
                row["name"], row["lot"]
            )
        )[:10]

        cards = {
            "within365": inventory_stat([row for row in filtered if not row["error"] and row["remaining"] is not None and 0 < row["remaining"] <= 365]),
            "within180": inventory_stat([row for row in filtered if not row["error"] and row["remaining"] is not None and 0 < row["remaining"] <= 180]),
            "within90": inventory_stat([row for row in filtered if not row["error"] and row["remaining"] is not None and 0 < row["remaining"] <= 90]),
            "expired": inventory_stat([row for row in filtered if not row["error"] and row["remaining"] is not None and row["remaining"] <= 0]),
        }

        return render_template(
            "dashboard_v2.html",
            dashboard={
                "snapshot": snapshot, "as_of": as_of,
                "filters": {
                    "q": request.args.get("q", "").strip(),
                    "warehouse": request.args.get("warehouse", "").strip(),
                    "availability": selected_availability,
                },
                "availability_labels": availability_labels,
                "all_warehouses": warehouses,
                "warehouses": warehouse_names,
                "products": products[:12],
                "product_count": len(products),
                "metrics": {
                    "total": total_qty,
                    "available": availability["available"]["quantity"],
                    "unavailable": availability["unavailable"]["quantity"],
                    "unclassified": availability["unclassified"]["quantity"],
                    "warehouse_count": len(warehouse_names),
                },
                "composition": {
                    "total": comp_total, "items": product_composition, "gradient": product_gradient
                },
                "trend": [],
                "expiry": {
                    "cards": cards,
                    "composition": expiry_composition,
                    "gradient": expiry_gradient,
                    "products": expiry_products,
                    "lots": priority,
                    "total": expiry_total,
                },
                "stagnant": [],
                "stagnant_summary": {"m3": 0, "m6": 0, "m12": 0, "history_count": 0},
            },
            error=None,
        )

    @bp.get("/inventory-location")
    @login_required
    def inventory_location():
        refs, snapshot, as_of, _, warehouses, _, rows = current_view()
        labels = {"available": "가용재고", "unavailable": "비가용재고", "unclassified": "분류 필요"}
        positive = [row for row in rows if number(row["quantity"]) > 0]
        summary = availability_stats(positive, refs)
        for row in positive:
            row["availability"], row["availability_source"] = warehouse_classification(row.get("warehouse"), refs)
            row["availability_label"] = labels[row["availability"]]
        selected = request.args.get("availability", "")
        if selected and selected not in labels:
            abort(400)
        filtered = [row for row in positive if not selected or row["availability"] == selected]
        groups = inventory_product_groups(filtered, refs)
        page = max(1, request.args.get("page", 1, type=int))
        pages = max(1, (len(filtered) + 99) // 100)
        page = min(page, pages)
        args = request.args.to_dict(); args.pop("page", None)
        return render_template("inventory_location.html", snapshot=snapshot, as_of=as_of, rows=filtered[(page-1)*100:page*100],
                               total=len(filtered), page=page, pages=pages, groups=groups[:100], summary=summary,
                               warehouses=warehouses, selected=selected, labels=labels,
                               previous_url=url_for("expiry.inventory_location", **args, page=page-1),
                               next_url=url_for("expiry.inventory_location", **args, page=page+1))

    @bp.route("/warehouse-classes", methods=["GET", "POST"])
    @roles("admin", "editor")
    def warehouse_classes():
        if request.method == "POST":
            names = request.form.getlist("warehouse_name")
            classifications = request.form.getlist("classification")
            if len(names) != len(classifications):
                abort(400)
            lock_writes()
            for name, classification in zip(names, classifications):
                name = str(name or "").strip()
                if not name or classification not in {"available", "unavailable", "unclassified"}:
                    abort(400)
                key = warehouse_key(name)
                row = db.session.scalar(select(Reference).where(Reference.kind == "warehouse_classes", Reference.key == key))
                if classification == "unclassified":
                    if row:
                        db.session.delete(row)
                    continue
                if not row:
                    row = Reference(kind="warehouse_classes", key=key)
                    db.session.add(row)
                row.payload = {"name": name, "classification": classification}
                row.updated_by = current_user.id
                row.updated_at = datetime.now(timezone.utc)
            audit("warehouse_classes_updated", detail=f"{len(names)} warehouses", commit=False)
            db.session.commit()
            flash("창고별 가용재고 분류를 저장했습니다.", "success")
            return redirect(url_for("expiry.warehouse_classes"))
        refs = references()
        snapshot = latest_snapshot()
        products, _ = stock_scope(snapshot.payload if snapshot else [])
        names = sorted({entry["data"].get("warehouse") or "미지정" for entry in products})
        rows = []
        for name in names:
            classification, source = warehouse_classification(name, refs)
            rows.append({"name": name, "classification": classification, "source": source})
        return render_template("warehouse_classes.html", rows=rows, snapshot=snapshot)

    @bp.get("/master-data")
    @login_required
    def master_data():
        refs = references()
        kinds = [(kind, FORMATS[kind][0], len(refs.get(kind, {}))) for kind in FORMATS if kind != "stock"]
        return render_template("expiry_master_data.html", kinds=kinds,
                               family_rule_count=len(refs.get("family_rules", {})))

    @bp.get("/stock-history")
    @login_required
    def stock_history():
        imports = db.session.scalars(select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None))
                                     .order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(100)).all()
        return render_template("expiry_stock_history.html", imports=imports, latest=latest_snapshot())

    @bp.get("/analysis")
    @login_required
    def analysis():
        _, snapshot, as_of, summary, _, _, rows = current_view()
        active = active_rows(rows)
        buckets = inventory_buckets(active)
        quality = data_quality_stats(active)
        warehouses = []
        ea_rows = [row for row in active if is_positive_ea(row)]
        for warehouse in sorted({row["warehouse"] or "미지정" for row in ea_rows}):
            group = [row for row in ea_rows if (row["warehouse"] or "미지정") == warehouse]
            warehouses.append({"name": warehouse, "total": inventory_stat(group),
                               "expired": inventory_stat([row for row in group if action_bucket(row) == "만료"]),
                               "due90": inventory_stat([row for row in group if action_bucket(row) == "1~90일"]),
                               "due180": inventory_stat([row for row in group if action_bucket(row) == "91~180일"]),
                               "issues": inventory_stat([row for row in group if action_bucket(row) == "확인 필요"])})
        warehouses.sort(key=lambda row: (row["expired"]["quantity"], row["due90"]["quantity"],
                                           row["issues"]["quantity"], row["total"]["quantity"]), reverse=True)
        product_groups = {}
        for row in active:
            if not row["error"] and (row["remaining"] is None or row["remaining"] > 365):
                continue
            key = (row["erp"], row["name"], row.get("spec") or "규격 미등록", row["unit"])
            group = product_groups.setdefault(key, {"erp": row["erp"], "name": row["name"],
                                                     "spec": row.get("spec") or "규격 미등록", "unit": row["unit"],
                                                     "lot_keys": set(), "quantity": number(0), "expired": 0,
                                                     "due90": 0, "issues": 0, "nearest": None})
            group["lot_keys"].add((row["erp"], row["lot"] or (row["warehouse"], row["location"])))
            group["quantity"] += number(row["quantity"])
            bucket = action_bucket(row)
            group["expired"] += bucket == "만료"
            group["due90"] += bucket == "1~90일"
            group["issues"] += bucket == "확인 필요"
            if row["remaining"] is not None:
                group["nearest"] = row["remaining"] if group["nearest"] is None else min(group["nearest"], row["remaining"])
        for group in product_groups.values():
            group["lots"] = len(group.pop("lot_keys"))
        products = sorted(product_groups.values(), key=lambda group: (0 if group["expired"] else 1,
                           0 if group["due90"] else 1, 0 if group["issues"] else 1,
                           group["nearest"] if group["nearest"] is not None else 999999,
                           group["name"]))[:30]
        factories = []
        for label in sorted({(row["factory"] + "공장") if row["factory"] else "미확인" for row in ea_rows}):
            group = [row for row in ea_rows if ((row["factory"] + "공장") if row["factory"] else "미확인") == label]
            factories.append({"label": label, **inventory_stat(group)})
        issues = []
        for label in sorted({row["error"] for row in ea_rows if row["error"]}):
            group = [row for row in ea_rows if row["error"] == label]
            issues.append({"label": label, **inventory_stat(group)})
        return render_template("expiry_analysis_data.html", snapshot=snapshot, as_of=as_of, summary=summary,
                               total=len(active), buckets=buckets, warehouses=warehouses, products=products,
                               factories=factories, issues=issues, quality=quality)

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
        active = active_rows(rows)
        selected_bucket = request.args.get("bucket", "")
        selected_bucket_label = dict(bucket_definitions + [
            ("all_ea", "전체 양수 EA 재고"), ("negative", "음수재고"),
            ("non_ea", "EA 외·단위 미확인 재고"),
        ]).get(selected_bucket)
        return render_template("expiry.html", rows=rows[(page-1)*100:page*100], total=len(rows), page=page, pages=pages,
                               previous_url=url_for("expiry.index", **args, page=page-1), next_url=url_for("expiry.index", **args, page=page+1),
                               export_url=url_for("expiry.export", **args), history=history, snapshot=snapshot,
                               as_of=as_of, summary=summary, warehouses=warehouses, statuses=statuses,
                               inventory_buckets=inventory_buckets(active), inventory_total=inventory_stat(active),
                               quality=data_quality_stats(active),
                               selected_bucket=selected_bucket, selected_bucket_label=selected_bucket_label,
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
