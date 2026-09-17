from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from flask import request
from sqlalchemy import select

from expiry_engine import calculate, family_rules, index_mts, iso_date, number, stock_scope

KST = ZoneInfo("Asia/Seoul")
DEFAULT_AVAILABLE = {"완제품창고", "3공장완제품창고"}
AVAILABILITY = {"available": "가용재고", "unavailable": "불용재고", "unclassified": "분류 필요"}
EXPIRY_BANDS = [
    ("over24", "24개월 이상", "#17689a"),
    ("m12_24", "12~24개월", "#2f91c5"),
    ("m6_12", "6~12개월", "#62bdd6"),
    ("m3_6", "3~6개월", "#62cdb4"),
    ("due90", "3개월 이내", "#f2c85c"),
    ("expired", "유효기간 만료", "#ef7777"),
]
PRODUCT_COLORS = ["#17689a", "#3f9fd0", "#65c3d0", "#91d2c6", "#f0aaa6"]


def warehouse_key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def warehouse_classification(name, refs):
    key = warehouse_key(name)
    custom = refs.get("warehouse_classes", {}).get(key)
    if custom and custom.get("classification") in {"available", "unavailable"}:
        return custom["classification"]
    if key in DEFAULT_AVAILABLE:
        return "available"
    return "unclassified"


def get_references(db, Reference):
    refs = {"family_rules": family_rules({})}
    for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
        refs.setdefault(row.kind, {})[row.key] = row.payload
    return refs


def get_snapshot(db, Import):
    snapshot_id = request.args.get("snapshot", "").strip()
    stmt = select(Import).where(Import.kind == "stock", Import.committed_at.is_not(None))
    if snapshot_id:
        return db.session.scalar(stmt.where(Import.id == snapshot_id))
    return db.session.scalar(stmt.order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))


def positive(row):
    return number(row.get("quantity")) > 0


def positive_ea(row):
    return positive(row) and (row.get("unit") or "").strip().upper() == "EA"


def matches_filter(row, refs, filters):
    warehouse = row.get("warehouse") or ""
    if filters["warehouse"] and warehouse != filters["warehouse"]:
        return False
    classification = warehouse_classification(warehouse, refs)
    if filters["availability"] and classification != filters["availability"]:
        return False
    q = filters["q"].casefold()
    if q and not any(q in str(row.get(key, "")).casefold() for key in (
        "erp", "icube", "name", "spec", "lot", "warehouse", "location"
    )):
        return False
    return True


def filtered_calculated_rows(rows, refs, filters):
    result = []
    for source in rows:
        if not positive(source) or not matches_filter(source, refs, filters):
            continue
        row = dict(source)
        row["availability"] = warehouse_classification(row.get("warehouse"), refs)
        row["availability_label"] = AVAILABILITY[row["availability"]]
        result.append(row)
    return result


def inventory_matrix(rows, refs, limit=50):
    warehouses = sorted({row.get("warehouse") or "미지정" for row in rows})
    warehouse_columns = [{
        "name": name,
        "classification": warehouse_classification(name, refs),
        "label": AVAILABILITY[warehouse_classification(name, refs)],
    } for name in warehouses]
    groups = {}
    for row in rows:
        quantity = number(row["quantity"])
        unit = (row.get("unit") or "단위 미확인").strip() or "단위 미확인"
        key = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록", unit)
        group = groups.setdefault(key, {
            "erp": row.get("erp"), "icube": row.get("icube"), "name": key[1], "spec": key[2], "unit": unit,
            "total": Decimal(0), "available": Decimal(0), "unavailable": Decimal(0), "unclassified": Decimal(0),
            "by_warehouse": defaultdict(lambda: Decimal(0)),
        })
        warehouse = row.get("warehouse") or "미지정"
        classification = warehouse_classification(warehouse, refs)
        group["total"] += quantity
        group[classification] += quantity
        group["by_warehouse"][warehouse] += quantity
    products = []
    for group in groups.values():
        group["by_warehouse"] = dict(group["by_warehouse"])
        products.append(group)
    products.sort(key=lambda item: (-float(item["total"]), item["name"], item["spec"]))
    return warehouse_columns, products, products[:limit]


def product_composition(products):
    ea_products = [p for p in products if p["unit"].upper() == "EA" and p["total"] > 0]
    total = sum((p["total"] for p in ea_products), Decimal(0))
    top = ea_products[:4]
    items = []
    used = Decimal(0)
    for index, product in enumerate(top):
        used += product["total"]
        items.append({
            "name": product["name"], "spec": product["spec"], "quantity": product["total"],
            "percent": float(product["total"] * 100 / total) if total else 0.0,
            "color": PRODUCT_COLORS[index],
        })
    other = total - used
    if other > 0:
        items.append({"name": "기타", "spec": "", "quantity": other,
                      "percent": float(other * 100 / total), "color": PRODUCT_COLORS[4]})
    cursor = 0.0
    parts = []
    for item in items:
        start = cursor
        cursor += item["percent"]
        parts.append(f"{item['color']} {start:.3f}% {cursor:.3f}%")
    gradient = "conic-gradient(" + ",".join(parts) + ")" if parts else "#e7eef4"
    return {"total": total, "items": items, "gradient": gradient}


def expiry_band(row):
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


def expiry_data(rows):
    ea_rows = [row for row in rows if positive_ea(row)]

    def stat(predicate):
        group = [row for row in ea_rows if predicate(row)]
        return {
            "quantity": sum((number(row["quantity"]) for row in group), Decimal(0)),
            "lots": len({(row.get("erp"), row.get("lot"), row.get("warehouse"), row.get("location")) for row in group}),
        }

    cards = {
        "within365": stat(lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 365),
        "within180": stat(lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 180),
        "within90": stat(lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 90),
        "expired": stat(lambda r: not r.get("error") and r.get("remaining") is not None and r["remaining"] <= 0),
        "issues": stat(lambda r: bool(r.get("error")) or r.get("remaining") is None),
    }

    totals = {key: Decimal(0) for key, _, _ in EXPIRY_BANDS}
    product_groups = {}
    lots = {}
    for row in ea_rows:
        band = expiry_band(row)
        quantity = number(row["quantity"])
        if band in totals:
            totals[band] += quantity
        pkey = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록")
        product = product_groups.setdefault(pkey, {
            "erp": row.get("erp"), "name": pkey[1], "spec": pkey[2], "total": Decimal(0),
            "bands": {key: Decimal(0) for key, _, _ in EXPIRY_BANDS}, "issue": Decimal(0),
        })
        product["total"] += quantity
        if band in product["bands"]:
            product["bands"][band] += quantity
        else:
            product["issue"] += quantity

        if band in {"expired", "due90", "m3_6", "m6_12", "issue"}:
            lkey = (row.get("erp"), row.get("lot") or "LOT 없음", row.get("warehouse") or "미지정")
            lot = lots.setdefault(lkey, {
                "name": row.get("name") or "품명 미등록", "spec": row.get("spec") or "규격 미등록",
                "erp": row.get("erp"), "lot": row.get("lot") or "LOT 없음", "warehouse": row.get("warehouse") or "미지정",
                "quantity": Decimal(0), "expiry": row.get("expiry"), "remaining": row.get("remaining"),
                "error": row.get("error"), "band": band,
            })
            lot["quantity"] += quantity
            if row.get("remaining") is not None and (lot["remaining"] is None or row["remaining"] < lot["remaining"]):
                lot["remaining"] = row["remaining"]
                lot["expiry"] = row.get("expiry")
                lot["band"] = band

    total = sum(totals.values(), Decimal(0))
    composition = []
    cursor = 0.0
    parts = []
    for key, label, color in EXPIRY_BANDS:
        quantity = totals[key]
        percent = float(quantity * 100 / total) if total else 0.0
        start = cursor
        cursor += percent
        composition.append({"key": key, "label": label, "color": color, "quantity": quantity, "percent": percent})
        parts.append(f"{color} {start:.3f}% {cursor:.3f}%")
    gradient = "conic-gradient(" + ",".join(parts) + ")" if parts else "#e7eef4"

    products = []
    for product in product_groups.values():
        segments = []
        for key, label, color in EXPIRY_BANDS:
            quantity = product["bands"][key]
            segments.append({"key": key, "label": label, "color": color, "quantity": quantity,
                             "percent": float(quantity * 100 / product["total"]) if product["total"] else 0.0})
        product["segments"] = segments
        product["risk"] = product["bands"]["expired"] + product["bands"]["due90"] + product["bands"]["m3_6"] + product["bands"]["m6_12"]
        products.append(product)
    products.sort(key=lambda p: (-float(p["risk"]), -float(p["total"]), p["name"]))

    risk_lots = list(lots.values())
    order = {"expired": 0, "due90": 1, "m3_6": 2, "m6_12": 3, "issue": 4}
    risk_lots.sort(key=lambda r: (order.get(r["band"], 9), r["remaining"] if r["remaining"] is not None else 999999,
                                  -float(r["quantity"]), r["name"]))
    return {"cards": cards, "composition": composition, "gradient": gradient,
            "products": products[:10], "risk_lots": risk_lots[:10], "total": total}


def raw_rows(snapshot):
    products, _ = stock_scope(snapshot.payload if snapshot else [])
    return [entry["data"] for entry in products]


def raw_total_ea(snapshot, refs, filters):
    total = Decimal(0)
    for row in raw_rows(snapshot):
        if number(row.get("quantity")) <= 0 or (row.get("unit") or "").strip().upper() != "EA":
            continue
        if matches_filter(row, refs, filters):
            total += number(row["quantity"])
    return total


def load_history(db, Import, snapshot, limit=48):
    if not snapshot or not snapshot.as_of:
        return []
    rows = db.session.scalars(select(Import).where(
        Import.kind == "stock", Import.committed_at.is_not(None), Import.as_of <= snapshot.as_of
    ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(limit)).all()
    unique = {}
    for row in rows:
        unique.setdefault(row.as_of, row)
    return [unique[key] for key in sorted(unique)]


def stock_trend(history, refs, filters):
    points = []
    for snap in history[-6:]:
        points.append({"date": snap.as_of, "label": snap.as_of.strftime("%m.%d"), "quantity": raw_total_ea(snap, refs, filters)})
    maximum = max([p["quantity"] for p in points] + [Decimal(1)])
    for point in points:
        point["percent"] = max(2.0, float(point["quantity"] * 100 / maximum)) if point["quantity"] else 0.0
    return points


def snapshot_aggregate(snapshot):
    result = defaultdict(lambda: Decimal(0))
    for row in raw_rows(snapshot):
        quantity = number(row.get("quantity"))
        if quantity <= 0:
            continue
        key = (row.get("warehouse") or "미지정", row.get("erp"), row.get("name") or "품명 미등록",
               row.get("spec") or "규격 미등록", (row.get("unit") or "단위 미확인").strip() or "단위 미확인")
        result[key] += quantity
    return dict(result)


def stagnation_data(history, snapshot, current_rows, refs, filters):
    if not history or not snapshot:
        return [], {"m3": 0, "m6": 0, "m12": 0, "from": None}
    history_maps = [(snap.as_of, snapshot_aggregate(snap)) for snap in history]
    current = defaultdict(lambda: Decimal(0))
    meta = {}
    for row in current_rows:
        if not matches_filter(row, refs, filters):
            continue
        key = (row.get("warehouse") or "미지정", row.get("erp"), row.get("name") or "품명 미등록",
               row.get("spec") or "규격 미등록", (row.get("unit") or "단위 미확인").strip() or "단위 미확인")
        current[key] += number(row["quantity"])
        meta[key] = {"warehouse": key[0], "erp": key[1], "name": key[2], "spec": key[3], "unit": key[4]}

    result = []
    for key, quantity in current.items():
        series = [(date, values.get(key, Decimal(0))) for date, values in history_maps]
        if not series:
            continue
        idx = len(series) - 1
        while idx > 0 and series[idx - 1][1] == quantity:
            idx -= 1
        unchanged_since = series[idx][0]
        first_idx = len(series) - 1
        while first_idx > 0 and series[first_idx - 1][1] > 0:
            first_idx -= 1
        first_seen = series[first_idx][0]
        days = max(0, (snapshot.as_of - unchanged_since).days)
        item = dict(meta[key])
        item.update({
            "quantity": quantity, "first_seen": first_seen, "unchanged_since": unchanged_since,
            "stagnant_days": days, "stagnant_months": round(days / 30.44, 1),
            "coverage_limited": idx == 0 and series[0][1] == quantity,
            "is_consignment": "수탁" in key[0] or "위탁" in key[0],
            "status": "12개월+" if days >= 365 else "6개월+" if days >= 180 else "3개월+" if days >= 90 else "관찰중",
        })
        result.append(item)
    result.sort(key=lambda r: (0 if r["is_consignment"] else 1, -r["stagnant_days"], -float(r["quantity"]), r["name"]))
    return result[:15], {
        "m3": sum(r["stagnant_days"] >= 90 for r in result),
        "m6": sum(r["stagnant_days"] >= 180 for r in result),
        "m12": sum(r["stagnant_days"] >= 365 for r in result),
        "from": history[0].as_of if history else None,
    }


def register_dashboard_context(app, db):
    @app.context_processor
    def dashboard_context_v2():
        if request.endpoint != "expiry.dashboard":
            return {}
        models = app.extensions.get("expiry_models", {})
        Reference = models.get("Reference")
        Import = models.get("Import")
        if Reference is None or Import is None:
            return {"dashboard2": {"snapshot": None}}
        refs = get_references(db, Reference)
        snapshot = get_snapshot(db, Import)
        if snapshot is None:
            return {"dashboard2": {"snapshot": None}}

        filters = {
            "warehouse": request.args.get("warehouse", "").strip(),
            "availability": request.args.get("availability", "").strip(),
            "q": request.args.get("q", "").strip(),
        }
        if filters["availability"] not in AVAILABILITY:
            filters["availability"] = ""
        try:
            as_of = iso_date(request.args.get("as_of") or datetime.now(timezone.utc).astimezone(KST).date().isoformat())
        except Exception:
            as_of = datetime.now(timezone.utc).astimezone(KST).date()

        refs_indexed = index_mts(refs)
        source_rows = raw_rows(snapshot)
        calculated = [calculate(row, refs_indexed, as_of) for row in source_rows]
        filtered = filtered_calculated_rows(calculated, refs, filters)
        all_warehouses = sorted({row.get("warehouse") or "미지정" for row in calculated if positive(row)})
        warehouse_columns, all_products, display_products = inventory_matrix(filtered, refs)
        ea_rows = [row for row in filtered if positive_ea(row)]
        total_ea = sum((number(row["quantity"]) for row in ea_rows), Decimal(0))
        available_ea = sum((number(row["quantity"]) for row in ea_rows if row["availability"] == "available"), Decimal(0))
        unavailable_ea = sum((number(row["quantity"]) for row in ea_rows if row["availability"] == "unavailable"), Decimal(0))
        unclassified_ea = sum((number(row["quantity"]) for row in ea_rows if row["availability"] == "unclassified"), Decimal(0))

        history = load_history(db, Import, snapshot)
        expiry = expiry_data(filtered)
        stagnation, stagnation_summary = stagnation_data(history, snapshot, [row for row in source_rows if positive(row)], refs, filters)
        composition = product_composition(all_products)
        return {"dashboard2": {
            "snapshot": snapshot, "as_of": as_of, "filters": filters, "availability_labels": AVAILABILITY,
            "warehouses": all_warehouses, "warehouse_columns": warehouse_columns,
            "products": display_products, "product_total_count": len(all_products),
            "metrics": {"total_qty": total_ea, "product_count": len(all_products), "warehouse_count": len(warehouse_columns),
                        "available_qty": available_ea, "unavailable_qty": unavailable_ea, "unclassified_qty": unclassified_ea},
            "product_composition": composition, "stock_trend": stock_trend(history, refs, filters),
            "expiry_cards": expiry["cards"], "expiry_composition": expiry["composition"],
            "expiry_gradient": expiry["gradient"], "expiry_total": expiry["total"],
            "expiry_products": expiry["products"], "risk_lots": expiry["risk_lots"],
            "stagnation": stagnation, "stagnation_summary": stagnation_summary,
        }}
