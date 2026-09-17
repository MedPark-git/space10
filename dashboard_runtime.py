from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import request

from expiry_engine import calculate, index_mts, iso_date, number
from dashboard_feature_v2 import (
    AVAILABILITY,
    expiry_data,
    filtered_calculated_rows,
    get_references,
    get_snapshot,
    inventory_matrix,
    load_history,
    positive,
    positive_ea,
    product_composition,
    raw_rows,
    stagnation_data,
    stock_trend,
)

KST = ZoneInfo("Asia/Seoul")


def build_dashboard_data(app, db):
    models = app.extensions.get("expiry_models", {})
    Reference = models.get("Reference")
    Import = models.get("Import")
    if Reference is None or Import is None:
        return {"snapshot": None, "error": "expiry_models_not_ready"}

    refs = get_references(db, Reference)
    snapshot = get_snapshot(db, Import)
    if snapshot is None:
        return {"snapshot": None, "error": "no_committed_stock_snapshot"}

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

    indexed = index_mts(refs)
    source_rows = raw_rows(snapshot)
    calculated = [calculate(row, indexed, as_of) for row in source_rows]
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
    positive_source_rows = [row for row in source_rows if positive(row)]
    stagnation, stagnation_summary = stagnation_data(history, snapshot, positive_source_rows, refs, filters)
    composition = product_composition(all_products)

    return {
        "snapshot": snapshot,
        "as_of": as_of,
        "filters": filters,
        "availability_labels": AVAILABILITY,
        "warehouses": all_warehouses,
        "warehouse_columns": warehouse_columns,
        "products": display_products,
        "product_total_count": len(all_products),
        "metrics": {
            "total_qty": total_ea,
            "product_count": len(all_products),
            "warehouse_count": len(warehouse_columns),
            "available_qty": available_ea,
            "unavailable_qty": unavailable_ea,
            "unclassified_qty": unclassified_ea,
        },
        "product_composition": composition,
        "stock_trend": stock_trend(history, refs, filters),
        "expiry_cards": expiry["cards"],
        "expiry_composition": expiry["composition"],
        "expiry_gradient": expiry["gradient"],
        "expiry_total": expiry["total"],
        "expiry_products": expiry["products"],
        "risk_lots": expiry["risk_lots"],
        "stagnation": stagnation,
        "stagnation_summary": stagnation_summary,
        "debug": {
            "snapshot_id": snapshot.id,
            "snapshot_date": snapshot.as_of,
            "source_product_rows": len(source_rows),
            "calculated_rows": len(calculated),
            "filtered_rows": len(filtered),
        },
    }
