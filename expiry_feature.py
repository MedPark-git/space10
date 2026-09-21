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
from product_display_master import lookup as product_display_lookup
from product_display_admin import register_product_display_admin


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
        locations = sorted({(r.get("location") or "").strip() for r in rows}, key=lambda value: (not value, value))
        summary["filter_locations"] = locations
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
        selected_warehouses = [value for value in request.args.getlist("warehouse") if value]
        selected_location_tokens = [value for value in request.args.getlist("location") if value]
        selected_locations = {
            "" if value == "__unassigned__" else value
            for value in selected_location_tokens
        }

        def matches_query(row):
            if not q:
                return True
            display = product_display_lookup(row.get("icube"), row.get("name"), row.get("spec"), refs=refs)
            values = (
                row.get("erp"), row.get("icube"), row.get("name"), row.get("spec"),
                row.get("lot"), row.get("warehouse"), row.get("location"),
                display.get("name"), display.get("type"), display.get("size"), display.get("category"),
            )
            return any(q in str(value or "").casefold() for value in values)

        def matches_product_factory(row):
            from inventory_spec_view import factory_for, FACTORIES
            selected = request.args.get("product_factory", "")
            if selected not in FACTORIES:
                return True
            shown = product_display_lookup(row.get("icube"), row.get("name"), row.get("spec"), refs=refs)
            return factory_for(row.get("icube") or "", shown.get("name"), row, refs) == selected

        filtered = [r for r in rows if
                    (request.args.get("zero") == "1" or number(r["quantity"]) != 0)
                    and in_bucket(r)
                    and (not selected_warehouses or r["warehouse"] in selected_warehouses)
                    and (not selected_locations or (r.get("location") or "").strip() in selected_locations)
                    and (not request.args.get("factory") or r["factory"] == request.args["factory"])
                    and (not request.args.get("status") or r["status"] == request.args["status"])
                    and matches_query(r) and matches_product_factory(r)]
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

    def dashboard_natural_key(value):
        parts = re.split(r"(\d+(?:\.\d+)?)", str(value or "").casefold())
        result = []
        for part in parts:
            if not part:
                continue
            try:
                result.append((0, float(part)))
            except ValueError:
                result.append((1, part))
        return result

    def dashboard_product_factory(display_name, row):
        if display_name in {"MedParkAlloD", "S1-Allo 덴탈"}:
            return "3"
        factory = (row.get("factory") or "").strip()
        if factory == "3":
            return "3"
        if factory in {"1·2", "1,2", "1/2", "1", "2"}:
            return "12"
        return "unknown"

    def dashboard_product_order(refs, factory_key):
        payload = refs.get("product_order", {}).get("dashboard", {})
        if not isinstance(payload, dict):
            return []
        order = payload.get("factory3" if factory_key == "3" else "factory12", [])
        return [str(name) for name in order if str(name).strip()]

    def cost_index(refs):
        result = {}
        for payload in refs.get("unit_cost", {}).values():
            if not isinstance(payload, dict):
                continue
            icube = str(payload.get("icube") or "").strip().upper()
            month = str(payload.get("month") or "").strip()
            try:
                cost = number(payload.get("cost"))
            except Exception:
                continue
            if not icube or not re.fullmatch(r"\d{4}-\d{2}", month) or cost < 0:
                continue
            result.setdefault(icube, []).append((month, cost))
        for values in result.values():
            values.sort(key=lambda item: item[0])
        return result

    def effective_unit_cost(indexed_costs, icube, as_of):
        code = str(icube or "").strip().upper()
        target_month = as_of.strftime("%Y-%m")
        candidates = [item for item in indexed_costs.get(code, []) if item[0] <= target_month]
        if not candidates:
            return None, None
        month, cost = candidates[-1]
        return cost, month

    def dashboard_inventory_data():
        refs, snapshot, as_of, summary, warehouses_from_view, _, rows = current_view()
        availability_labels = {
            "available": "가용재고",
            "unavailable": "불용재고",
            "unclassified": "분류 필요",
        }
        selected_availability = request.args.get("availability", "").strip()
        if selected_availability not in availability_labels:
            selected_availability = ""
        selected_product_factory = request.args.get("product_factory", "").strip()
        if selected_product_factory not in {"12", "3"}:
            selected_product_factory = ""
        show_value = request.args.get("show_value") == "1"
        indexed_costs = cost_index(refs) if show_value else {}

        def stock_bucket(row):
            warehouse = warehouse_key(row.get("warehouse"))
            location = warehouse_key(row.get("location"))
            classification, _ = warehouse_classification(row.get("warehouse"), refs)
            if classification == "unavailable":
                return "unusable"
            if warehouse == warehouse_key("완제품창고"):
                return "finished12"
            if warehouse == warehouse_key("3공장 완제품창고"):
                return "finished3"
            if "공정중" in warehouse:
                if "3공장" in location:
                    return "work3"
                if "1공장" in location:
                    return "work1"
            return "other"

        q = request.args.get("q", "").strip().casefold()
        positive_ea = [row for row in rows if is_positive_ea(row)]
        filtered = []
        for source_row in positive_ea:
            row = dict(source_row)
            row["availability"], row["availability_source"] = warehouse_classification(row.get("warehouse"), refs)
            row["availability_label"] = availability_labels[row["availability"]]
            display = product_display_lookup(row.get("icube"), row.get("name"), row.get("spec"), refs=refs)
            row["display_name"] = display["name"]
            row["display_type"] = display["type"]
            row["display_size"] = display["size"]
            row["display_category"] = display["category"]
            row["display_mapped"] = display["mapped"]
            row["product_factory"] = dashboard_product_factory(row["display_name"], row)
            row["stock_bucket"] = stock_bucket(row)
            row["location_exception"] = False
            if row["product_factory"] == "3" and row["stock_bucket"] in {"finished12", "work1"}:
                row["stock_bucket"] = "other"
                row["location_exception"] = True
            if show_value:
                try:
                    unit_cost, cost_month = effective_unit_cost(indexed_costs, row.get("icube"), as_of)
                except Exception:
                    unit_cost, cost_month = None, None
                row["unit_cost"] = unit_cost
                row["cost_month"] = cost_month
                row["stock_value"] = number(row["quantity"]) * unit_cost if unit_cost is not None else None
            else:
                row["unit_cost"] = None
                row["cost_month"] = None
                row["stock_value"] = None
            if selected_product_factory and row["product_factory"] != selected_product_factory:
                continue
            if selected_availability and row["availability"] != selected_availability:
                continue
            if q and not any(q in str(value or "").casefold() for value in (
                row.get("erp"), row.get("icube"), row.get("name"), row.get("spec"),
                row.get("display_name"), row.get("display_type"), row.get("display_size"),
                row.get("display_category"), row.get("warehouse"), row.get("location")
            )):
                continue
            filtered.append(row)

        groups = {}
        location_exception_qty = number(0)
        missing_cost_codes = set()
        for row in filtered:
            if row["location_exception"]:
                location_exception_qty += number(row["quantity"])
            if show_value and row["unit_cost"] is None and row.get("icube"):
                missing_cost_codes.add(str(row["icube"]).strip().upper())

            name = row["display_name"]
            product = groups.setdefault(name, {
                "name": name,
                "factories": set(),
                "total": number(0),
                "available": number(0),
                "other": number(0),
                "unusable": number(0),
                "finished12": number(0),
                "finished3": number(0),
                "work1": number(0),
                "work3": number(0),
                "location_exception": number(0),
                "value": number(0),
                "available_value": number(0),
                "other_value": number(0),
                "unusable_value": number(0),
                "value_incomplete": False,
                "rows": {},
                "mapped": True,
            })
            product["factories"].add(row["product_factory"])
            qty = number(row["quantity"])
            product["total"] += qty
            if row["stock_value"] is None:
                product["value_incomplete"] = True
            else:
                product["value"] += row["stock_value"]
            if not row["display_mapped"]:
                product["mapped"] = False
            if row["stock_bucket"] == "unusable":
                product["unusable"] += qty
                if row["stock_value"] is not None:
                    product["unusable_value"] += row["stock_value"]
            elif row["stock_bucket"] == "other":
                product["other"] += qty
                if row["stock_value"] is not None:
                    product["other_value"] += row["stock_value"]
            else:
                product["available"] += qty
                product[row["stock_bucket"]] += qty
                if row["stock_value"] is not None:
                    product["available_value"] += row["stock_value"]
            if row["location_exception"]:
                product["location_exception"] += qty

            row_key = (row["display_category"], row["display_type"], row["display_size"])
            line = product["rows"].setdefault(row_key, {
                "category": row["display_category"],
                "type": row["display_type"],
                "size": row["display_size"],
                "available": number(0),
                "other": number(0),
                "unusable": number(0),
                "finished12": number(0),
                "finished3": number(0),
                "work1": number(0),
                "work3": number(0),
                "location_exception": number(0),
                "total": number(0),
                "value": number(0),
                "available_value": number(0),
                "other_value": number(0),
                "unusable_value": number(0),
                "value_incomplete": False,
            })
            line["total"] += qty
            if row["stock_value"] is None:
                line["value_incomplete"] = True
            else:
                line["value"] += row["stock_value"]
            if row["stock_bucket"] == "unusable":
                line["unusable"] += qty
                if row["stock_value"] is not None:
                    line["unusable_value"] += row["stock_value"]
            elif row["stock_bucket"] == "other":
                line["other"] += qty
                if row["stock_value"] is not None:
                    line["other_value"] += row["stock_value"]
            else:
                line["available"] += qty
                line[row["stock_bucket"]] += qty
                if row["stock_value"] is not None:
                    line["available_value"] += row["stock_value"]
            if row["location_exception"]:
                line["location_exception"] += qty

        order12 = {name: index for index, name in enumerate(dashboard_product_order(refs, "12"))}
        order3 = {name: index for index, name in enumerate(dashboard_product_order(refs, "3"))}

        product_groups = []
        for product in groups.values():
            product["rows"] = list(product["rows"].values())
            product["rows"].sort(key=lambda line: (
                dashboard_natural_key(line["category"]),
                dashboard_natural_key(line["type"]),
                dashboard_natural_key(line["size"]),
            ))
            factories = product.pop("factories")
            if factories == {"3"}:
                product["factory_key"] = "3"
                product["factory_label"] = "3공장 제품"
            elif factories == {"12"}:
                product["factory_key"] = "12"
                product["factory_label"] = "1·2공장 제품"
            elif factories == {"unknown"}:
                product["factory_key"] = "unknown"
                product["factory_label"] = "공장 미분류"
            else:
                product["factory_key"] = "mixed"
                product["factory_label"] = "공장 혼합"
            product_groups.append(product)

        def product_sort_key(group):
            if group["factory_key"] == "12":
                return (0, order12.get(group["name"], 999999), dashboard_natural_key(group["name"]))
            if group["factory_key"] == "3":
                return (1, order3.get(group["name"], 999999), dashboard_natural_key(group["name"]))
            return (2, 999999, dashboard_natural_key(group["name"]))

        product_groups.sort(key=product_sort_key)
        groups12 = [group for group in product_groups if group["factory_key"] == "12"]
        groups3 = [group for group in product_groups if group["factory_key"] == "3"]
        groups_other = [group for group in product_groups if group["factory_key"] not in {"12", "3"}]

        total_qty = sum((group["total"] for group in product_groups), number(0))
        available_total = sum((group["available"] for group in product_groups), number(0))
        other_total = sum((group["other"] for group in product_groups), number(0))
        unusable_total = sum((group["unusable"] for group in product_groups), number(0))
        total_value = sum((group["value"] for group in product_groups), number(0))
        available_value = sum((group["available_value"] for group in product_groups), number(0))
        other_value = sum((group["other_value"] for group in product_groups), number(0))
        unusable_value = sum((group["unusable_value"] for group in product_groups), number(0))
        master_unmapped = len({row.get("icube") or row.get("erp") for row in filtered if not row.get("display_mapped")})

        def summary_row(group):
            return {
                "name": group["name"],
                "factory_key": group["factory_key"],
                "factory_label": group["factory_label"],
                "available": group["available"],
                "other": group["other"],
                "unusable": group["unusable"],
                "total": group["total"],
                "finished12": group["finished12"],
                "finished3": group["finished3"],
                "work1": group["work1"],
                "work3": group["work3"],
                "location_exception": group["location_exception"],
                "value": group["value"],
                "available_value": group["available_value"],
                "other_value": group["other_value"],
                "unusable_value": group["unusable_value"],
                "value_incomplete": group["value_incomplete"],
            }

        return {
            "snapshot": snapshot,
            "as_of": as_of,
            "summary": summary,
            "filters": {
                "q": request.args.get("q", "").strip(),
                "warehouses": [value for value in request.args.getlist("warehouse") if value],
                "availability": selected_availability,
                "product_factory": selected_product_factory,
                "show_value": show_value,
            },
            "availability_labels": availability_labels,
            "all_warehouses": warehouses_from_view,
            "product_groups": product_groups,
            "groups12": groups12,
            "groups3": groups3,
            "groups_other": groups_other,
            "product_summary12": [summary_row(group) for group in groups12],
            "product_summary3": [summary_row(group) for group in groups3],
            "product_summary_other": [summary_row(group) for group in groups_other],
            "metrics": {
                "total": total_qty,
                "available": available_total,
                "other": other_total,
                "unusable": unusable_total,
                "total_value": total_value,
                "available_value": available_value,
                "other_value": other_value,
                "unusable_value": unusable_value,
                "product_groups": len(product_groups),
                "master_unmapped": master_unmapped,
                "missing_cost": len(missing_cost_codes),
                "location_exception": location_exception_qty,
            },
        }

    @bp.route("/product-order", methods=["GET", "POST"])
    @roles("admin", "editor")
    def product_order():
        refs = references()
        snapshot = latest_snapshot()
        as_of = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date()
        indexed = index_mts(refs)
        products, _ = stock_scope(snapshot.payload if snapshot else [])
        names12, names3 = set(), set()
        for entry in products:
            raw = entry["data"]
            try:
                if number(raw.get("quantity")) <= 0:
                    continue
                calc = calculate(raw, indexed, as_of, tuple(refs.get("settings", {}).get("alerts", {}).get("days", [90, 180, 365])))
                display = product_display_lookup(calc.get("icube"), calc.get("name"), calc.get("spec"), refs=refs)
                factory = dashboard_product_factory(display["name"], calc)
                if factory == "3":
                    names3.add(display["name"])
                elif factory == "12":
                    names12.add(display["name"])
            except Exception:
                continue

        def ordered_names(names, factory):
            saved = dashboard_product_order(refs, factory)
            result = [name for name in saved if name in names]
            result.extend(sorted((name for name in names if name not in result), key=dashboard_natural_key))
            return result

        ordered12 = ordered_names(names12, "12")
        ordered3 = ordered_names(names3, "3")

        if request.method == "POST":
            submitted12 = [name.strip() for name in request.form.getlist("product_name_12") if name.strip()]
            submitted3 = [name.strip() for name in request.form.getlist("product_name_3") if name.strip()]
            if set(submitted12) != set(ordered12) or len(submitted12) != len(set(submitted12)):
                abort(400)
            if set(submitted3) != set(ordered3) or len(submitted3) != len(set(submitted3)):
                abort(400)
            lock_writes()
            row = db.session.scalar(select(Reference).where(Reference.kind == "product_order", Reference.key == "dashboard"))
            if row is None:
                row = Reference(kind="product_order", key="dashboard")
                db.session.add(row)
            row.payload = {"factory12": submitted12, "factory3": submitted3}
            row.updated_by = current_user.id
            row.updated_at = datetime.now(timezone.utc)
            audit("product_order_updated", detail=f"12:{len(submitted12)}, 3:{len(submitted3)}", commit=False)
            db.session.commit()
            flash("공장별 제품 표시 순서를 저장했습니다.", "success")
            return redirect(url_for("expiry.product_order"))

        return render_template("product_order.html", products12=ordered12, products3=ordered3, snapshot=snapshot)

    @bp.route("/unit-costs", methods=["GET", "POST"])
    @roles("admin", "editor")
    def unit_costs():
        refs = references()
        current_month = request.args.get("month", "").strip() or datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", current_month):
            abort(400)

        if request.method == "POST":
            month = request.form.get("month", "").strip()
            pasted = request.form.get("pasted", "")
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                flash("적용월은 YYYY-MM 형식으로 입력해 주세요.", "error")
                return redirect(url_for("expiry.unit_costs"))
            parsed = []
            errors = []
            for line_no, raw_line in enumerate(pasted.splitlines(), 1):
                line = raw_line.strip()
                if not line:
                    continue
                cells = [cell.strip() for cell in re.split(r"\t|,", line)]
                if len(cells) < 2:
                    errors.append(f"{line_no}행: ICUBE 품번과 제조원가가 필요합니다.")
                    continue
                icube = cells[0].upper()
                cost_text = cells[1].replace(",", "").replace("원", "").strip()
                if line_no == 1 and ("품번" in icube or "ICUBE" in icube) and ("원가" in cells[1] or "COST" in cells[1].upper()):
                    continue
                try:
                    cost = number(cost_text)
                except Exception:
                    errors.append(f"{line_no}행: 제조원가를 숫자로 입력해 주세요.")
                    continue
                if not icube or cost < 0:
                    errors.append(f"{line_no}행: 품번 또는 제조원가를 확인해 주세요.")
                    continue
                parsed.append((icube, cost))
            if errors:
                flash(" / ".join(errors[:5]) + (" 외 오류가 더 있습니다." if len(errors) > 5 else ""), "error")
                return render_template("unit_costs.html", month=month, rows=[], pasted=pasted)
            if not parsed:
                flash("저장할 제조원가가 없습니다.", "error")
                return redirect(url_for("expiry.unit_costs", month=month))

            lock_writes()
            for icube, cost in parsed:
                key = f"{month}|{icube}"
                row = db.session.scalar(select(Reference).where(Reference.kind == "unit_cost", Reference.key == key))
                if row is None:
                    row = Reference(kind="unit_cost", key=key)
                    db.session.add(row)
                row.payload = {"month": month, "icube": icube, "cost": str(cost)}
                row.updated_by = current_user.id
                row.updated_at = datetime.now(timezone.utc)
            audit("unit_costs_updated", detail=f"{month}: {len(parsed)} items", commit=False)
            db.session.commit()
            flash(f"{month} 제조원가 {len(parsed)}건을 저장했습니다.", "success")
            return redirect(url_for("expiry.unit_costs", month=month))

        rows = []
        for payload in refs.get("unit_cost", {}).values():
            if isinstance(payload, dict) and payload.get("month") == current_month:
                rows.append({
                    "icube": payload.get("icube"),
                    "cost": number(payload.get("cost")),
                })
        rows.sort(key=lambda item: dashboard_natural_key(item["icube"]))
        months = sorted({
            str(payload.get("month"))
            for payload in refs.get("unit_cost", {}).values()
            if isinstance(payload, dict) and payload.get("month")
        }, reverse=True)
        return render_template("unit_costs.html", month=current_month, rows=rows, months=months, pasted="")

    @bp.get("/dashboard")
    @login_required
    def dashboard():
        return render_template("inventory_dashboard.html", dashboard=dashboard_inventory_data())

    @bp.get("/dashboard-expiry")
    @login_required
    def dashboard_expiry():
        from expiry_display import render_expiry_display
        return render_expiry_display(current_view)


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
        args = request.args.to_dict()
        args.pop("page", None)
        history = db.session.scalars(
            select(Import)
            .where(Import.kind == "stock", Import.committed_at.is_not(None))
            .order_by(Import.as_of.desc(), Import.committed_at.desc())
            .limit(100)
        ).all()
        active = active_rows(rows)
        selected_bucket = request.args.get("bucket", "")
        selected_bucket_label = dict(bucket_definitions + [
            ("all_ea", "전체 양수 EA 재고"),
            ("negative", "음수재고"),
            ("non_ea", "EA 외·단위 미확인 재고"),
        ]).get(selected_bucket)
        return render_template(
            "expiry.html",
            rows=rows[(page-1)*100:page*100],
            total=len(rows),
            page=page,
            pages=pages,
            summary=summary,
            warehouses=warehouses,
            statuses=statuses,
            history=history,
            active=active,
            selected_bucket=selected_bucket,
            selected_bucket_label=selected_bucket_label,
            previous_url=url_for("expiry.index", **args, page=max(1, page-1)),
            next_url=url_for("expiry.index", **args, page=min(pages, page+1)),
        )

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
        columns = [
            ("warehouse", "창고"), ("location", "장소"), ("erp", "아마란스 품번"), ("icube", "ICUBE 품번"),
            ("name", "품명"), ("spec", "규격"), ("account", "계정구분"), ("lot", "LOT"), ("unit", "단위"),
            ("quantity", "기말재고"), ("factory", "공장"), ("expiry", "사용기한"), ("remaining", "잔여일"),
            ("status", "상태"), ("error", "확인사항"), ("source", "계산근거"),
        ]
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow([label for _, label in columns])
        for row in rows:
            cells = []
            for key, _ in columns:
                value = "" if row.get(key) is None else str(row[key])
                if key not in {"quantity", "remaining"} and (
                    value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
                ):
                    value = "'" + value
                cells.append(value)
            writer.writerow(cells)
        return Response(
            "\ufeff" + stream.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=inventory_expiry.csv"},
        )

    register_product_display_admin(bp, db, audit, roles, Reference, references, lock_writes)
    app.register_blueprint(bp)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.config["MAX_FORM_MEMORY_SIZE"] = 10 * 1024 * 1024
    app.extensions["expiry_models"] = {"Reference": Reference, "Import": Import}
