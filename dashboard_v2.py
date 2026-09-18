from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from flask import render_template, request
from flask_login import login_required
from sqlalchemy import select

from expiry_engine import calculate, family_rules, index_mts, number, stock_scope

KST = ZoneInfo("Asia/Seoul")
DEFAULT_AVAILABLE = {"완제품창고", "3공장완제품창고"}
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
    ("issue", "확인 필요", "#9b8ac4"),
]
PRODUCT_COLORS = ["#17689a", "#3e96c8", "#5db5d2", "#86cfc4", "#f0aaa6"]


def _wkey(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _references(db, Reference):
    refs = {"family_rules": family_rules({})}
    for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
        refs.setdefault(row.kind, {})[row.key] = row.payload
    return refs


def _warehouse_class(name, refs):
    key = _wkey(name)
    custom = refs.get("warehouse_classes", {}).get(key)
    if custom and custom.get("classification") in {"available", "unavailable"}:
        return custom["classification"]
    if key in DEFAULT_AVAILABLE:
        return "available"
    return "unclassified"


def _latest_snapshot(db, Import):
    return db.session.scalar(
        select(Import)
        .where(Import.kind == "stock", Import.committed_at.is_not(None))
        .order_by(Import.as_of.desc(), Import.committed_at.desc())
        .limit(1)
    )


def _history(db, Import, snapshot, limit=24):
    if snapshot is None:
        return []
    rows = db.session.scalars(
        select(Import)
        .where(
            Import.kind == "stock",
            Import.committed_at.is_not(None),
            Import.as_of <= snapshot.as_of,
        )
        .order_by(Import.as_of.desc(), Import.committed_at.desc())
        .limit(limit * 3)
    ).all()
    unique = {}
    for row in rows:
        unique.setdefault(row.as_of, row)
    return [unique[d] for d in sorted(unique)][-limit:]


def _raw_product_rows(snapshot):
    products, scope = stock_scope(snapshot.payload if snapshot else [])
    return [entry["data"] for entry in products], scope


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
    q = request.args.get("q", "").strip().casefold()
    warehouse = request.args.get("warehouse", "").strip()
    availability = request.args.get("availability", "").strip()
    if availability not in AVAILABILITY_LABELS:
        availability = ""
    result = []
    for source in rows:
        if not _is_positive_ea(source):
            continue
        row = dict(source)
        row["availability"] = _warehouse_class(row.get("warehouse"), refs)
        if warehouse and (row.get("warehouse") or "") != warehouse:
            continue
        if availability and row["availability"] != availability:
            continue
        if q and not any(
            q in str(row.get(key, "")).casefold()
            for key in ("erp", "icube", "name", "spec", "lot", "warehouse", "location")
        ):
            continue
        result.append(row)
    return result, {"q": request.args.get("q", "").strip(), "warehouse": warehouse, "availability": availability}


def _inventory_products(rows):
    warehouses = sorted({row.get("warehouse") or "미지정" for row in rows})
    groups = {}
    for row in rows:
        key = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록")
        group = groups.setdefault(
            key,
            {
                "erp": row.get("erp"),
                "icube": row.get("icube"),
                "name": key[1],
                "spec": key[2],
                "total": Decimal(0),
                "available": Decimal(0),
                "unavailable": Decimal(0),
                "unclassified": Decimal(0),
                "by_warehouse": defaultdict(lambda: Decimal(0)),
            },
        )
        qty = number(row["quantity"])
        wh = row.get("warehouse") or "미지정"
        group["total"] += qty
        group[row["availability"]] += qty
        group["by_warehouse"][wh] += qty
    products = []
    for group in groups.values():
        group["by_warehouse"] = dict(group["by_warehouse"])
        products.append(group)
    products.sort(key=lambda x: (-float(x["total"]), x["name"], x["spec"]))
    return warehouses, products


def _composition(products):
    total = sum((p["total"] for p in products), Decimal(0))
    items = []
    used = Decimal(0)
    for idx, p in enumerate(products[:4]):
        used += p["total"]
        items.append(
            {
                "name": p["name"],
                "spec": p["spec"],
                "quantity": p["total"],
                "percent": float(p["total"] * 100 / total) if total else 0.0,
                "color": PRODUCT_COLORS[idx],
            }
        )
    other = total - used
    if other > 0:
        items.append(
            {
                "name": "기타",
                "spec": "",
                "quantity": other,
                "percent": float(other * 100 / total) if total else 0.0,
                "color": PRODUCT_COLORS[4],
            }
        )
    cursor = 0.0
    gradient = []
    for item in items:
        start = cursor
        cursor += item["percent"]
        gradient.append(f'{item["color"]} {start:.3f}% {cursor:.3f}%')
    return {
        "total": total,
        "items": items,
        "gradient": "conic-gradient(" + ",".join(gradient) + ")" if gradient else "#e9eef4",
    }


def _expiry(rows):
    cards = {}
    predicates = {
        "within365": lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 365,
        "within180": lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 180,
        "within90": lambda r: not r.get("error") and r.get("remaining") is not None and 0 < r["remaining"] <= 90,
        "expired": lambda r: not r.get("error") and r.get("remaining") is not None and r["remaining"] <= 0,
    }
    for key, predicate in predicates.items():
        group = [r for r in rows if predicate(r)]
        cards[key] = {
            "quantity": sum((number(r["quantity"]) for r in group), Decimal(0)),
            "lots": len({(r.get("erp"), r.get("lot"), r.get("warehouse"), r.get("location")) for r in group}),
        }

    totals = {key: Decimal(0) for key, _, _ in EXPIRY_BANDS}
    product_groups = {}
    for row in rows:
        band = _expiry_band(row)
        qty = number(row["quantity"])
        totals[band] += qty
        pkey = (row.get("erp"), row.get("name") or "품명 미등록", row.get("spec") or "규격 미등록")
        group = product_groups.setdefault(
            pkey,
            {
                "erp": row.get("erp"),
                "name": pkey[1],
                "spec": pkey[2],
                "total": Decimal(0),
                "bands": {key: Decimal(0) for key, _, _ in EXPIRY_BANDS},
            },
        )
        group["total"] += qty
        group["bands"][band] += qty

    total = sum(totals.values(), Decimal(0))
    composition = []
    cursor = 0.0
    gradient = []
    for key, label, color in EXPIRY_BANDS:
        qty = totals[key]
        percent = float(qty * 100 / total) if total else 0.0
        start = cursor
        cursor += percent
        composition.append({"key": key, "label": label, "color": color, "quantity": qty, "percent": percent})
        if percent > 0:
            gradient.append(f"{color} {start:.3f}% {cursor:.3f}%")

    products = []
    for group in product_groups.values():
        segments = []
        for key, label, color in EXPIRY_BANDS:
            qty = group["bands"][key]
            segments.append(
                {
                    "key": key,
                    "label": label,
                    "color": color,
                    "quantity": qty,
                    "percent": float(qty * 100 / group["total"]) if group["total"] else 0.0,
                }
            )
        group["segments"] = segments
        group["risk"] = (
            group["bands"]["expired"]
            + group["bands"]["due90"]
            + group["bands"]["m3_6"]
            + group["bands"]["m6_12"]
            + group["bands"]["issue"]
        )
        products.append(group)
    products.sort(key=lambda x: (-float(x["risk"]), -float(x["total"]), x["name"]))

    lots = []
    for row in rows:
        if row.get("remaining") is None and not row.get("error"):
            continue
        lots.append(
            {
                "name": row.get("name") or "품명 미등록",
                "spec": row.get("spec") or "규격 미등록",
                "erp": row.get("erp"),
                "lot": row.get("lot") or "LOT 없음",
                "warehouse": row.get("warehouse") or "미지정",
                "quantity": number(row["quantity"]),
                "expiry": row.get("expiry"),
                "remaining": row.get("remaining"),
                "error": row.get("error"),
                "band": _expiry_band(row),
            }
        )
    lots.sort(
        key=lambda x: (
            x["remaining"] if x["remaining"] is not None else 999999,
            -float(x["quantity"]),
            x["name"],
        )
    )
    return {
        "cards": cards,
        "composition": composition,
        "gradient": "conic-gradient(" + ",".join(gradient) + ")" if gradient else "#e9eef4",
        "products": products,
        "lots": lots[:10],
        "total": total,
    }


def _snapshot_totals(history, refs, filters):
    points = []
    for snapshot in history[-6:]:
        raw, _ = _raw_product_rows(snapshot)
        total = Decimal(0)
        for row in raw:
            if not _is_positive_ea(row):
                continue
            wh = row.get("warehouse") or ""
            classification = _warehouse_class(wh, refs)
            if filters["warehouse"] and wh != filters["warehouse"]:
                continue
            if filters["availability"] and classification != filters["availability"]:
                continue
            q = filters["q"].casefold()
            if q and not any(q in str(row.get(k, "")).casefold() for k in ("erp", "name", "spec", "warehouse")):
                continue
            total += number(row["quantity"])
        points.append({"date": snapshot.as_of, "label": snapshot.as_of.strftime("%m.%d"), "quantity": total})
    maximum = max([p["quantity"] for p in points] + [Decimal(1)])
    for p in points:
        p["height"] = float(p["quantity"] * 100 / maximum) if maximum else 0.0
    return points


def _aggregate_snapshot(snapshot):
    result = defaultdict(lambda: Decimal(0))
    raw, _ = _raw_product_rows(snapshot)
    for row in raw:
        if not _is_positive_ea(row):
            continue
        key = (
            row.get("warehouse") or "미지정",
            row.get("erp"),
            row.get("name") or "품명 미등록",
            row.get("spec") or "규격 미등록",
        )
        result[key] += number(row["quantity"])
    return dict(result)


def _stagnant(history, snapshot, current_rows, filters):
    if not history or snapshot is None:
        return [], {"m3": 0, "m6": 0, "m12": 0, "history_count": 0}
    maps = [(s.as_of, _aggregate_snapshot(s)) for s in history]
    current = defaultdict(lambda: Decimal(0))
    for row in current_rows:
        key = (
            row.get("warehouse") or "미지정",
            row.get("erp"),
            row.get("name") or "품명 미등록",
            row.get("spec") or "규격 미등록",
        )
        current[key] += number(row["quantity"])

    rows = []
    for key, qty in current.items():
        wh, erp, name, spec = key
        if filters["warehouse"] and wh != filters["warehouse"]:
            continue
        q = filters["q"].casefold()
        if q and q not in f"{erp} {name} {spec} {wh}".casefold():
            continue
        series = [(date, values.get(key, Decimal(0))) for date, values in maps]
        idx = len(series) - 1
        while idx > 0 and series[idx - 1][1] == qty:
            idx -= 1
        unchanged_since = series[idx][0]
        days = max(0, (snapshot.as_of - unchanged_since).days)
        rows.append(
            {
                "warehouse": wh,
                "erp": erp,
                "name": name,
                "spec": spec,
                "quantity": qty,
                "unchanged_since": unchanged_since,
                "days": days,
                "months": round(days / 30.44, 1),
                "consignment": "수탁" in wh or "위탁" in wh,
                "limited": idx == 0,
            }
        )
    rows.sort(key=lambda x: (0 if x["consignment"] else 1, -x["days"], -float(x["quantity"]), x["name"]))
    return rows[:10], {
        "m3": sum(r["days"] >= 90 for r in rows),
        "m6": sum(r["days"] >= 180 for r in rows),
        "m12": sum(r["days"] >= 365 for r in rows),
        "history_count": len(history),
    }


def register_dashboard_v2(app, db):
    @app.get("/dashboard-v2")
    @login_required
    def dashboard_v2():
        try:
            models = app.extensions.get("expiry_models", {})
            Reference = models.get("Reference")
            Import = models.get("Import")
            if Reference is None or Import is None:
                return render_template("dashboard_v2.html", dashboard=None, error="기준 모델을 불러오지 못했습니다.")

            snapshot = _latest_snapshot(db, Import)
            if snapshot is None:
                return render_template("dashboard_v2.html", dashboard=None, error=None)

            refs = _references(db, Reference)
            raw_rows, scope = _raw_product_rows(snapshot)
            indexed = index_mts(refs)
            as_of = datetime.now(timezone.utc).astimezone(KST).date()
            calculated = [calculate(row, indexed, as_of) for row in raw_rows]
            filtered, filters = _apply_filters(calculated, refs)
            all_warehouses = sorted({r.get("warehouse") or "미지정" for r in calculated if _is_positive_ea(r)})
            warehouses, products = _inventory_products(filtered)

            total = sum((number(r["quantity"]) for r in filtered), Decimal(0))
            availability = {
                key: sum((number(r["quantity"]) for r in filtered if r["availability"] == key), Decimal(0))
                for key in AVAILABILITY_LABELS
            }
            history = _history(db, Import, snapshot)
            expiry = _expiry(filtered)
            stagnant, stagnant_summary = _stagnant(history, snapshot, filtered, filters)

            dashboard = {
                "snapshot": snapshot,
                "as_of": as_of,
                "scope": scope,
                "filters": filters,
                "availability_labels": AVAILABILITY_LABELS,
                "all_warehouses": all_warehouses,
                "warehouses": warehouses,
                "products": products[:12],
                "product_count": len(products),
                "metrics": {
                    "total": total,
                    "available": availability["available"],
                    "unavailable": availability["unavailable"],
                    "unclassified": availability["unclassified"],
                    "warehouse_count": len(warehouses),
                },
                "composition": _composition(products),
                "trend": _snapshot_totals(history, refs, filters),
                "expiry": expiry,
                "stagnant": stagnant,
                "stagnant_summary": stagnant_summary,
            }
            return render_template("dashboard_v2.html", dashboard=dashboard, error=None)
        except Exception:
            app.logger.exception("dashboard_v2 rendering failed")
            db.session.rollback()
            return render_template(
                "dashboard_v2.html",
                dashboard=None,
                error="대시보드 계산 중 오류가 발생했습니다. 기존 재고·유효기간 데이터는 변경되지 않았습니다.",
            )
