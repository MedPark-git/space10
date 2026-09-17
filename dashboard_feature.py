from collections import defaultdict
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from flask import request
from sqlalchemy import select

from expiry_engine import calculate, family_rules, index_mts, iso_date, number, stock_scope


KST = ZoneInfo("Asia/Seoul")
DEFAULT_AVAILABLE_WAREHOUSE_KEYS = {"완제품창고", "3공장완제품창고"}
AVAILABILITY_LABELS = {
    "available": "가용재고",
    "unavailable": "불용재고",
    "unclassified": "분류 필요",
}
EXPIRY_BANDS = [
    ("over24", "24개월 이상", "#17689a"),
    ("m12_24", "12~24개월", "#2f91c5"),
    ("m6_12", "6~12개월", "#62bdd6"),
    ("m3_6", "3~6개월", "#62cdb4"),
    ("due90", "3개월 이내", "#f2c85c"),
    ("expired", "유효기간 만료", "#ef7777"),
]


def _warehouse_key(name):
    return re.sub(r"\s+", "", str(name or "")).casefold()


def _warehouse_classification(name, refs):
    key = _warehouse_key(name)
    custom = refs.get("warehouse_classes", {}).get(key)
    if custom and custom.get("classification") in {"available", "unavailable"}:
        return custom["classification"]
    if key in DEFAULT_AVAILABLE_WAREHOUSE_KEYS:
        return "available"
    return "unclassified"


def _references(db, Reference):
    refs = {"family_rules": family_rules({})}
    for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
        refs.setdefault(row.kind, {})[row.key] = row.payload
    return refs


def _current_snapshot(db, Import):
    snapshot_id = request.args.get("snapshot", "").strip()
    stmt = select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None))
    if snapshot_id:
        return db.session.scalar(stmt.where(Import.id == snapshot_id))
    return db.session.scalar(stmt.order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))


def _positive_rows(rows):
    return [row for row in rows if number(row.get("quantity")) > 0]


def _is_positive_ea(row):
    return number(row.get("quantity")) > 0 and (row.get("unit") or "").strip().upper() == "EA"


def _expiry_band(row):
    if row.get("error") or row.get("remaining") is None:
        return "issue"
    remaining = row["remaining"]
    if remaining <= 0:
        return "expired"
    if remaining <= 90:
        return "due90"
    if remaining <= 180:
        return "m3_6"
    if remaining <= 365:
        return "m6_12"
    if remaining <= 730:
        return "m12_24"
    return "over24"


def _apply_filters(rows, refs):
    warehouse = request.args.get("warehouse", "").strip()
    availability = request.args.get("availability", "").strip()
    q = request.args.get("q", "").strip().casefold()
    filtered = []
    for row in rows:
        row = dict(row)
        row["availability"] = _warehouse_classification(row.get("warehouse"), refs)
        row["availability_label"] = AVAILABILITY_LABELS[row["availability"]]
        if warehouse and (row.get("warehouse") or "") != warehouse:
            continue
        if availability and row["availability"] != availability:
            continue
        if q and not any(q in str(row.get(key, "")).casefold() for key in (
            "erp", "icube", "name", "spec", "lot", "warehouse", "location"
        )):
            continue
        filtered.append(row)
    return filtered


def _inventory_matrix(rows, refs):
    warehouses = sorted({row.get("warehouse") or "미지정" for row in rows if number(row.get("quantity")) > 0})
    warehouse_columns = [
        {
            "name": name,
            "classification": _warehouse_classification(name, refs),
            "label": AVAILABILITY_LABELS[_warehouse_classification(name, refs)],
        }
        for name in warehouses
    ]
    groups = {}
    for row in rows:
        quantity = number(row.get("quantity"))
        if quantity <= 0:
            continue
        unit = (row.get("unit") or "단위 미확인").strip() or "단위 미확인"
        key = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록", unit)
        group = groups.setdefault(key, {
            "erp": row.get("erp"),
            "icube": row.get("icube"),
            "name": row.get("name") or "품명 미등록",
            "spec": row.get("spec") or "규격 미등록",
            "unit": unit,
            "total": number(0),
            "available": number(0),
            "unavailable": number(0),
            "unclassified": number(0),
            "by_warehouse": defaultdict(lambda: number(0)),
        })
        warehouse = row.get("warehouse") or "미지정"
        classification = _warehouse_classification(warehouse, refs)
        group["total"] += quantity
        group[classification] += quantity
        group["by_warehouse"][warehouse] += quantity
    result = []
    for group in groups.values():
        group["by_warehouse"] = dict(group["by_warehouse"])
        result.append(group)
    result.sort(key=lambda item: (-float(item["total"]), item["name"], item["spec"]))
    return warehouse_columns, result


def _expiry_summary(rows):
    ea_rows = [row for row in rows if _is_positive_ea(row)]
    expired = [row for row in ea_rows if not row.get("error") and row.get("remaining") is not None and row["remaining"] <= 0]
    due90 = [row for row in ea_rows if not row.get("error") and row.get("remaining") is not None and 0 < row["remaining"] <= 90]
    due180 = [row for row in ea_rows if not row.get("error") and row.get("remaining") is not None and 0 < row["remaining"] <= 180]
    due365 = [row for row in ea_rows if not row.get("error") and row.get("remaining") is not None and 0 < row["remaining"] <= 365]
    issues = [row for row in ea_rows if row.get("error") or row.get("remaining") is None]

    def stat(group):
        return {
            "quantity": sum((number(row["quantity"]) for row in group), number(0)),
            "lots": len({(row.get("erp"), row.get("lot"), row.get("warehouse"), row.get("location")) for row in group}),
        }

    cards = {
        "within365": stat(due365),
        "within180": stat(due180),
        "within90": stat(due90),
        "expired": stat(expired),
        "issues": stat(issues),
    }

    band_totals = {key: number(0) for key, _, _ in EXPIRY_BANDS}
    for row in ea_rows:
        band = _expiry_band(row)
        if band in band_totals:
            band_totals[band] += number(row["quantity"])
    band_total = sum(band_totals.values(), number(0))
    composition = []
    cursor = 0.0
    gradient_parts = []
    for key, label, color in EXPIRY_BANDS:
        quantity = band_totals[key]
        percent = float(quantity * 100 / band_total) if band_total else 0.0
        start = cursor
        cursor += percent
        composition.append({
            "key": key,
            "label": label,
            "color": color,
            "quantity": quantity,
            "percent": percent,
            "start": start,
            "end": cursor,
        })
        gradient_parts.append(f"{color} {start:.3f}% {cursor:.3f}%")
    gradient = "conic-gradient(" + ",".join(gradient_parts) + ")" if band_total else "#e8eef4"

    product_groups = {}
    for row in ea_rows:
        key = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록")
        group = product_groups.setdefault(key, {
            "erp": row.get("erp"),
            "name": row.get("name") or "품명 미등록",
            "spec": row.get("spec") or "규격 미등록",
            "total": number(0),
            "bands": {band_key: number(0) for band_key, _, _ in EXPIRY_BANDS},
            "issue": number(0),
            "nearest": None,
            "nearest_expiry": None,
        })
        quantity = number(row["quantity"])
        group["total"] += quantity
        band = _expiry_band(row)
        if band in group["bands"]:
            group["bands"][band] += quantity
        else:
            group["issue"] += quantity
        if row.get("remaining") is not None and (group["nearest"] is None or row["remaining"] < group["nearest"]):
            group["nearest"] = row["remaining"]
            group["nearest_expiry"] = row.get("expiry")

    products = []
    for group in product_groups.values():
        group["segments"] = []
        for key, label, color in EXPIRY_BANDS:
            quantity = group["bands"][key]
            percent = float(quantity * 100 / group["total"]) if group["total"] else 0.0
            group["segments"].append({"key": key, "label": label, "color": color, "quantity": quantity, "percent": percent})
        group["risk_quantity"] = group["bands"]["expired"] + group["bands"]["due90"] + group["bands"]["m3_6"] + group["bands"]["m6_12"]
        products.append(group)
    products.sort(key=lambda item: (-float(item["risk_quantity"]), -float(item["total"]), item["name"]))

    lot_groups = {}
    for row in ea_rows:
        band = _expiry_band(row)
        if band not in {"expired", "due90", "m3_6", "m6_12", "issue"}:
            continue
        key = (
            row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록",
            row.get("lot") or "LOT 없음", row.get("warehouse") or "미지정"
        )
        group = lot_groups.setdefault(key, {
            "erp": row.get("erp"), "name": row.get("name") or "품명 미등록",
            "spec": row.get("spec") or "규격 미등록", "lot": row.get("lot") or "LOT 없음",
            "warehouse": row.get("warehouse") or "미지정", "quantity": number(0),
            "expiry": row.get("expiry"), "remaining": row.get("remaining"), "error": row.get("error"),
            "band": band,
        })
        group["quantity"] += number(row["quantity"])
        if row.get("remaining") is not None and (group["remaining"] is None or row["remaining"] < group["remaining"]):
            group["remaining"] = row["remaining"]
            group["expiry"] = row.get("expiry")
            group["band"] = band
    lots = list(lot_groups.values())
    lots.sort(key=lambda item: (
        0 if item["band"] == "expired" else 1 if item["band"] == "due90" else 2 if item["band"] == "m3_6" else 3 if item["band"] == "m6_12" else 4,
        item["remaining"] if item["remaining"] is not None else 999999,
        -float(item["quantity"]),
    ))
    return cards, composition, gradient, products[:10], lots[:10]


def _aggregate_snapshot_payload(payload):
    products, _ = stock_scope(payload or [])
    result = defaultdict(lambda: number(0))
    for entry in products:
        row = entry["data"]
        quantity = number(row.get("quantity"))
        if quantity <= 0:
            continue
        key = (
            row.get("warehouse") or "미지정",
            row.get("erp"),
            row.get("name") or "품명 미등록",
            row.get("spec") or "규격 미등록",
            (row.get("unit") or "단위 미확인").strip() or "단위 미확인",
        )
        result[key] += quantity
    return dict(result)


def _stagnation(db, Import, snapshot, rows):
    if not snapshot or not snapshot.as_of:
        return [], {"m3": 0, "m6": 0, "m12": 0}
    stmt = select(Import).where(
        Import.kind == "stock",
        Import.committed_at.is_not(None),
        Import.as_of <= snapshot.as_of,
    ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(180)
    snapshots = db.session.scalars(stmt).all()
    by_date = {}
    for snap in snapshots:
        by_date.setdefault(snap.as_of, snap)
    ordered = [by_date[key] for key in sorted(by_date)]
    history = [(snap.as_of, _aggregate_snapshot_payload(snap.payload)) for snap in ordered]

    current = defaultdict(lambda: number(0))
    meta = {}
    for row in rows:
        quantity = number(row.get("quantity"))
        if quantity <= 0:
            continue
        key = (
            row.get("warehouse") or "미지정",
            row.get("erp"),
            row.get("name") or "품명 미등록",
            row.get("spec") or "규격 미등록",
            (row.get("unit") or "단위 미확인").strip() or "단위 미확인",
        )
        current[key] += quantity
        meta[key] = {
            "warehouse": key[0], "erp": key[1], "name": key[2], "spec": key[3], "unit": key[4]
        }

    results = []
    for key, current_qty in current.items():
        if not history:
            continue
        series = [(date, values.get(key, number(0))) for date, values in history]
        if series[-1][0] != snapshot.as_of:
            series.append((snapshot.as_of, current_qty))
        else:
            series[-1] = (series[-1][0], current_qty)
        idx = len(series) - 1
        while idx > 0 and series[idx - 1][1] == current_qty:
            idx -= 1
        unchanged_since = series[idx][0]
        coverage_limited = idx == 0 and series[0][1] == current_qty

        first_idx = len(series) - 1
        while first_idx > 0 and series[first_idx - 1][1] > 0:
            first_idx -= 1
        first_seen = series[first_idx][0]

        stagnant_days = max(0, (snapshot.as_of - unchanged_since).days)
        stagnant_months = round(stagnant_days / 30.44, 1)
        item = dict(meta[key])
        item.update({
            "quantity": current_qty,
            "first_seen": first_seen,
            "unchanged_since": unchanged_since,
            "stagnant_days": stagnant_days,
            "stagnant_months": stagnant_months,
            "coverage_limited": coverage_limited,
            "is_consignment": "수탁" in item["warehouse"] or "위탁" in item["warehouse"],
            "status": "12개월+" if stagnant_days >= 365 else "6개월+" if stagnant_days >= 180 else "3개월+" if stagnant_days >= 90 else "관찰중",
        })
        results.append(item)
    results.sort(key=lambda item: (0 if item["is_consignment"] else 1, -item["stagnant_days"], -float(item["quantity"]), item["name"]))
    summary = {
        "m3": sum(1 for item in results if item["stagnant_days"] >= 90),
        "m6": sum(1 for item in results if item["stagnant_days"] >= 180),
        "m12": sum(1 for item in results if item["stagnant_days"] >= 365),
    }
    return results[:12], summary


def register_dashboard_context(app, db):
    @app.context_processor
    def inventory_dashboard_context():
        if request.endpoint != "expiry.dashboard":
            return {}
        models = app.extensions.get("expiry_models", {})
        Reference = models.get("Reference")
        Import = models.get("Import")
        if not Reference or not Import:
            return {"dashboard2": {"snapshot": None}}

        refs = _references(db, Reference)
        snapshot = _current_snapshot(db, Import)
        if not snapshot:
            return {"dashboard2": {"snapshot": None}}

        try:
            as_of = iso_date(request.args.get("as_of") or datetime.now(timezone.utc).astimezone(KST).date().isoformat())
        except Exception:
            as_of = datetime.now(timezone.utc).astimezone(KST).date()

        indexed = index_mts(refs)
        products, scope = stock_scope(snapshot.payload or [])
        calculated = [calculate(entry["data"], indexed, as_of) for entry in products]
        positive_all = _positive_rows(calculated)
        all_warehouses = sorted({row.get("warehouse") or "미지정" for row in positive_all})
        filtered = _apply_filters(positive_all, refs)
        warehouse_columns, matrix = _inventory_matrix(filtered, refs)

        total_qty = sum((number(row["quantity"]) for row in filtered), number(0))
        ea_rows = [row for row in filtered if _is_positive_ea(row)]
        available_qty = sum((number(row["quantity"]) for row in filtered if row["availability"] == "available"), number(0))
        unavailable_qty = sum((number(row["quantity"]) for row in filtered if row["availability"] == "unavailable"), number(0))
        unclassified_qty = sum((number(row["quantity"]) for row in filtered if row["availability"] == "unclassified"), number(0))
        expiry_cards, expiry_composition, donut_gradient, expiry_products, risk_lots = _expiry_summary(filtered)
        stagnation, stagnation_summary = _stagnation(db, Import, snapshot, filtered)

        selected_availability = request.args.get("availability", "").strip()
        if selected_availability not in AVAILABILITY_LABELS:
            selected_availability = ""

        dashboard = {
            "snapshot": snapshot,
            "as_of": as_of,
            "scope": scope,
            "filters": {
                "warehouse": request.args.get("warehouse", "").strip(),
                "availability": selected_availability,
                "q": request.args.get("q", "").strip(),
            },
            "availability_labels": AVAILABILITY_LABELS,
            "warehouses": all_warehouses,
            "warehouse_columns": warehouse_columns,
            "products": matrix,
            "metrics": {
                "total_qty": total_qty,
                "product_count": len(matrix),
                "warehouse_count": len(warehouse_columns),
                "available_qty": available_qty,
                "unavailable_qty": unavailable_qty,
                "unclassified_qty": unclassified_qty,
                "ea_qty": sum((number(row["quantity"]) for row in ea_rows), number(0)),
            },
            "expiry_cards": expiry_cards,
            "expiry_composition": expiry_composition,
            "donut_gradient": donut_gradient,
            "expiry_products": expiry_products,
            "risk_lots": risk_lots,
            "stagnation": stagnation,
            "stagnation_summary": stagnation_summary,
        }
        return {"dashboard2": dashboard}
