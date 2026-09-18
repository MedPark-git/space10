import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import render_template, request
from flask_login import login_required
from sqlalchemy import select

from expiry_engine import calculate, family_rules, index_mts, number, stock_scope
from product_display_master import lookup as product_display_lookup


def make_inventory_dashboard_view(app, db):
    Reference = app.extensions["expiry_models"]["Reference"]
    Import = app.extensions["expiry_models"]["Import"]

    def references():
        result = {"family_rules": family_rules({})}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def warehouse_key(value):
        return re.sub(r"\s+", "", str(value or "")).casefold()

    def classify_warehouse(name, refs):
        key = warehouse_key(name)
        custom = refs.get("warehouse_classes", {}).get(key)
        if isinstance(custom, dict) and custom.get("classification") in {"available", "unavailable"}:
            return custom["classification"]
        if key in {warehouse_key("완제품창고"), warehouse_key("3공장완제품창고")}:
            return "available"
        return "unclassified"

    def product_factory(display_name, row):
        if display_name in {"MedParkAlloD", "S1-Allo 덴탈"}:
            return "3"
        factory = str(row.get("factory") or "").strip()
        if factory == "3":
            return "3"
        if factory in {"1", "2", "1·2", "1,2", "1/2"}:
            return "12"
        return "unknown"

    def stock_bucket(row, refs):
        warehouse = warehouse_key(row.get("warehouse"))
        location = warehouse_key(row.get("location"))
        classification = classify_warehouse(row.get("warehouse"), refs)
        if classification == "unavailable":
            return "unusable"
        if warehouse == warehouse_key("완제품창고"):
            return "finished12"
        if warehouse == warehouse_key("3공장완제품창고"):
            return "finished3"
        if "공정중" in warehouse:
            if "3공장" in location:
                return "work3"
            if "1공장" in location:
                return "work1"
        return "other"

    def natural_key(value):
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

    def product_order(refs, factory_key):
        payload = refs.get("product_order", {}).get("dashboard", {})
        if not isinstance(payload, dict):
            return []
        key = "factory3" if factory_key == "3" else "factory12"
        values = payload.get(key, [])
        return [str(v) for v in values if str(v).strip()]

    def build_cost_index(refs):
        result = {}
        for payload in refs.get("unit_cost", {}).values():
            if not isinstance(payload, dict):
                continue
            icube = str(payload.get("icube") or "").strip().upper()
            month = str(payload.get("month") or "").strip()
            if not icube or not re.fullmatch(r"\d{4}-\d{2}", month):
                continue
            try:
                cost = number(payload.get("cost"))
            except Exception:
                continue
            if cost < 0:
                continue
            result.setdefault(icube, []).append((month, cost))
        for values in result.values():
            values.sort(key=lambda item: item[0])
        return result

    def effective_cost(indexed, icube, as_of):
        code = str(icube or "").strip().upper()
        target = as_of.strftime("%Y-%m")
        values = [item for item in indexed.get(code, []) if item[0] <= target]
        if not values:
            return None
        return values[-1][1]

    @login_required
    def view():
        refs = references()
        snapshot = db.session.scalar(
            select(Import)
            .where(Import.kind == "stock", Import.committed_at.is_not(None))
            .order_by(Import.as_of.desc(), Import.committed_at.desc())
            .limit(1)
        )
        if snapshot is None:
            return render_template("inventory_dashboard.html", dashboard=None)

        as_of = snapshot.as_of or datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date()
        indexed = index_mts(refs)
        products, _ = stock_scope(snapshot.payload or [])
        rows = []
        for entry in products:
            try:
                row = calculate(entry["data"], indexed, as_of)
            except Exception:
                continue
            try:
                qty = number(row.get("quantity"))
            except Exception:
                continue
            if qty <= 0 or str(row.get("unit") or "").strip().upper() != "EA":
                continue
            rows.append(dict(row))

        selected_warehouses = [v for v in request.args.getlist("warehouse") if v]
        selected_factory = request.args.get("product_factory", "").strip()
        if selected_factory not in {"12", "3"}:
            selected_factory = ""
        selected_availability = request.args.get("availability", "").strip()
        if selected_availability not in {"available", "unavailable", "unclassified"}:
            selected_availability = ""
        query = request.args.get("q", "").strip().casefold()
        show_value = request.args.get("show_value") == "1"
        costs = build_cost_index(refs) if show_value else {}

        filtered = []
        for row in rows:
            display = product_display_lookup(row.get("icube"), row.get("name"), row.get("spec"))
            row["display_name"] = display.get("name") or row.get("name") or "제품명 미등록"
            row["display_type"] = display.get("type") or "-"
            row["display_size"] = display.get("size") or row.get("spec") or "-"
            row["display_category"] = display.get("category") or "-"
            row["display_mapped"] = bool(display.get("mapped"))
            row["product_factory"] = product_factory(row["display_name"], row)
            row["availability"] = classify_warehouse(row.get("warehouse"), refs)
            row["stock_bucket"] = stock_bucket(row, refs)
            row["location_exception"] = False
            if row["product_factory"] == "3" and row["stock_bucket"] in {"finished12", "work1"}:
                row["stock_bucket"] = "other"
                row["location_exception"] = True

            if selected_warehouses and row.get("warehouse") not in selected_warehouses:
                continue
            if selected_factory and row["product_factory"] != selected_factory:
                continue
            if selected_availability and row["availability"] != selected_availability:
                continue
            if query:
                haystack = [
                    row.get("erp"), row.get("icube"), row.get("name"), row.get("spec"),
                    row.get("display_name"), row.get("display_type"), row.get("display_size"),
                    row.get("display_category"), row.get("warehouse"), row.get("location")
                ]
                if not any(query in str(v or "").casefold() for v in haystack):
                    continue

            if show_value:
                try:
                    row["unit_cost"] = effective_cost(costs, row.get("icube"), as_of)
                except Exception:
                    row["unit_cost"] = None
            else:
                row["unit_cost"] = None
            row["stock_value"] = number(row["quantity"]) * row["unit_cost"] if row["unit_cost"] is not None else None
            filtered.append(row)

        groups = {}
        missing_cost = set()
        for row in filtered:
            qty = number(row["quantity"])
            if show_value and row["unit_cost"] is None and row.get("icube"):
                missing_cost.add(str(row["icube"]).strip().upper())

            name = row["display_name"]
            group = groups.setdefault(name, {
                "name": name,
                "factories": set(),
                "rows": {},
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
            })
            group["factories"].add(row["product_factory"])
            group["total"] += qty
            if row["stock_value"] is None:
                group["value_incomplete"] = show_value
            else:
                group["value"] += row["stock_value"]

            bucket = row["stock_bucket"]
            if bucket == "unusable":
                group["unusable"] += qty
                if row["stock_value"] is not None:
                    group["unusable_value"] += row["stock_value"]
            elif bucket == "other":
                group["other"] += qty
                if row["stock_value"] is not None:
                    group["other_value"] += row["stock_value"]
            else:
                group["available"] += qty
                group[bucket] += qty
                if row["stock_value"] is not None:
                    group["available_value"] += row["stock_value"]
            if row["location_exception"]:
                group["location_exception"] += qty

            key = (row["display_category"], row["display_type"], row["display_size"])
            line = group["rows"].setdefault(key, {
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
                "value_incomplete": False,
            })
            line["total"] += qty
            if row["stock_value"] is None:
                line["value_incomplete"] = show_value
            else:
                line["value"] += row["stock_value"]
            if bucket == "unusable":
                line["unusable"] += qty
            elif bucket == "other":
                line["other"] += qty
            else:
                line["available"] += qty
                line[bucket] += qty
            if row["location_exception"]:
                line["location_exception"] += qty

        order12 = {name: i for i, name in enumerate(product_order(refs, "12"))}
        order3 = {name: i for i, name in enumerate(product_order(refs, "3"))}
        product_groups = []

        for group in groups.values():
            group["rows"] = list(group["rows"].values())
            group["rows"].sort(key=lambda r: (natural_key(r["category"]), natural_key(r["type"]), natural_key(r["size"])))
            factories = group.pop("factories")
            if factories == {"3"}:
                group["factory_key"] = "3"
                group["factory_label"] = "3공장 제품"
            elif factories == {"12"}:
                group["factory_key"] = "12"
                group["factory_label"] = "1·2공장 제품"
            elif factories == {"unknown"}:
                group["factory_key"] = "unknown"
                group["factory_label"] = "공장 미분류"
            else:
                group["factory_key"] = "mixed"
                group["factory_label"] = "공장 혼합"
            product_groups.append(group)

        def sort_key(group):
            if group["factory_key"] == "12":
                return (0, order12.get(group["name"], 999999), natural_key(group["name"]))
            if group["factory_key"] == "3":
                return (1, order3.get(group["name"], 999999), natural_key(group["name"]))
            return (2, 999999, natural_key(group["name"]))

        product_groups.sort(key=sort_key)
        groups12 = [g for g in product_groups if g["factory_key"] == "12"]
        groups3 = [g for g in product_groups if g["factory_key"] == "3"]
        groups_other = [g for g in product_groups if g["factory_key"] not in {"12", "3"}]

        def summary(group):
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

        warehouses = sorted({row.get("warehouse") or "미지정" for row in rows})
        total = sum((g["total"] for g in product_groups), number(0))
        available = sum((g["available"] for g in product_groups), number(0))
        other = sum((g["other"] for g in product_groups), number(0))
        unusable = sum((g["unusable"] for g in product_groups), number(0))
        total_value = sum((g["value"] for g in product_groups), number(0))
        available_value = sum((g["available_value"] for g in product_groups), number(0))
        other_value = sum((g["other_value"] for g in product_groups), number(0))
        unusable_value = sum((g["unusable_value"] for g in product_groups), number(0))

        dashboard = {
            "snapshot": snapshot,
            "as_of": as_of,
            "filters": {
                "q": request.args.get("q", "").strip(),
                "warehouses": selected_warehouses,
                "availability": selected_availability,
                "product_factory": selected_factory,
                "show_value": show_value,
            },
            "availability_labels": {
                "available": "가용재고",
                "unavailable": "불용재고",
                "unclassified": "분류 필요",
            },
            "all_warehouses": warehouses,
            "product_groups": product_groups,
            "groups12": groups12,
            "groups3": groups3,
            "groups_other": groups_other,
            "product_summary12": [summary(g) for g in groups12],
            "product_summary3": [summary(g) for g in groups3],
            "product_summary_other": [summary(g) for g in groups_other],
            "metrics": {
                "total": total,
                "available": available,
                "other": other,
                "unusable": unusable,
                "total_value": total_value,
                "available_value": available_value,
                "other_value": other_value,
                "unusable_value": unusable_value,
                "product_groups": len(product_groups),
                "master_unmapped": len({row.get("icube") or row.get("erp") for row in filtered if not row.get("display_mapped")}),
                "missing_cost": len(missing_cost),
                "location_exception": sum((g["location_exception"] for g in product_groups), number(0)),
            },
        }
        return render_template("inventory_dashboard.html", dashboard=dashboard)

    return view
