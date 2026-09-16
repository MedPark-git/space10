"""Deterministic expiry calculations and tab-separated imports; no database access."""
import csv
import io
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


class InputError(ValueError):
    pass


FORMATS = {
    "stock": ("ERP 재고", "사업장\t창고\t장소\t품번\t품명\t규격\t재고단위\t계정구분\tLOT No.\t기말재고"),
    "mapping": ("품번 마스터", "아마란스 품번\tICUBE 품번"),
    "rules": ("유효기간 규칙", "공장\t앞자리 KEY\t끝자리 KEY\t유효일수\tMTS 제품구분"),
    "mts": ("MTS 사용기한", "제품명\t제조번호\t사용기한"),
    "exceptions": ("LOT 예외", "아마란스 품번\tLOT No.\t사용기한\t사유"),
}


def clean(value):
    return str(value or "").strip().lstrip("\ufeff")


def is_finished_product(row):
    return clean(row.get("account")) == "제품"


def stock_scope(entries):
    products = [e for e in entries if is_finished_product(e["data"])]
    return products, {"원본 행": len(entries), "집계 대상 제품": len(products),
                      "제품 외 제외": sum(bool(clean(e["data"].get("account"))) and not is_finished_product(e["data"]) for e in entries),
                      "계정구분 미확인 제외": sum(not clean(e["data"].get("account")) for e in entries)}


def iso_date(value):
    value = clean(value).replace(".", "-").replace("/", "-")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError("날짜는 YYYY-MM-DD 형식으로 입력해 주세요.") from exc


def number(value):
    value = clean(value).replace(",", "")
    try:
        n = Decimal(value or "0")
    except InvalidOperation as exc:
        raise InputError("수량을 숫자로 입력해 주세요.") from exc
    if not n.is_finite() or abs(n) >= Decimal("1000000000000") or n.as_tuple().exponent < -6:
        raise InputError("수량은 유한한 숫자, 소수점 6자리 이내여야 합니다.")
    return n


def mts_lot(value):
    value = clean(value).upper()
    if re.fullmatch(r"\d{6}[A-Z]{2}(?:\d{4})?", value):
        return value[:8]
    raise InputError("MTS 제조번호는 앞 8자리 LOT 또는 LOT+숫자 4자리 형식이어야 합니다.")


def parse_paste(kind, source):
    if kind not in FORMATS:
        raise InputError("지원하지 않는 자료입니다.")
    if len(source.encode("utf-8")) > 8 * 1024 * 1024:
        raise InputError("한 번에 8MB까지 붙여넣을 수 있습니다. 자료를 나누어 등록해 주세요.")
    rows = list(csv.reader(io.StringIO(source), delimiter="\t"))
    header_idx = None
    aliases = {
        "아마란스": "아마란스 품번", "아마란스품번": "아마란스 품번",
        "아이큐브": "ICUBE 품번", "아이큐브 품번": "ICUBE 품번", "i-cube 품번": "ICUBE 품번",
        "KEY값": "앞자리 KEY", "KEY값2": "끝자리 KEY", "MTS 사용기한": "사용기한",
        "MTS 제조번호": "제조번호", "LOT": "LOT No.", "제품구분": "제품명", "계정 구분": "계정구분",
    }
    required = {
        "stock": {"창고", "장소", "품번", "품명", "계정구분", "LOT No.", "기말재고"},
        "mapping": {"아마란스 품번", "ICUBE 품번"},
        "rules": {"앞자리 KEY", "끝자리 KEY"},
        "mts": {"제품명", "제조번호", "사용기한"},
        "exceptions": {"아마란스 품번", "LOT No.", "사용기한", "사유"},
    }[kind]
    for i, row in enumerate(rows[:60]):
        headers = [aliases.get(clean(v), clean(v)) for v in row]
        if kind == "mapping" and headers[:2] == ["품번", "품번"]:
            headers[:2] = ["아마란스 품번", "ICUBE 품번"]
        if required <= set(headers):
            header_idx = i
            break
    if header_idx is None:
        raise InputError("열 제목을 포함해서 붙여넣어 주세요. 필요한 열: " + ", ".join(sorted(required)))
    nonblank = [h for h in headers if h]
    if len(nonblank) != len(set(nonblank)):
        raise InputError("열 제목이 중복되어 자료를 구분할 수 없습니다.")
    entries, seen, notes = [], {}, Counter()
    reported_total = None
    for lineno, row in enumerate(rows[header_idx + 1:], header_idx + 2):
        if not any(clean(v) for v in row):
            continue
        if kind == "mapping" and (clean(row[0]) == "ITEM_CD" or clean(row[0]).startswith("타입 :")):
            continue
        if len(row) > len(headers) and any(clean(v) for v in row[len(headers):]):
            raise InputError(f"{lineno}행: 열 개수가 제목보다 많습니다.")
        d = {h: clean(row[j]) if j < len(row) else "" for j, h in enumerate(headers) if h}
        if kind == "stock" and not d.get("품번") and "합계" in row:
            reported_total = number(d.get("기말재고")); notes["합계행 제외"] += 1; continue
        try:
            if kind == "stock":
                if not d["품번"] or not d["품명"] or not d["창고"]:
                    raise InputError("품번·품명·창고가 필요합니다.")
                payload = {"erp": d["품번"], "name": d["품명"], "account": d["계정구분"], "lot": d["LOT No."].upper(),
                           "warehouse": d["창고"], "location": d["장소"], "business": d.get("사업장", ""),
                           "spec": d.get("규격", ""), "unit": d.get("재고단위", ""), "quantity": str(number(d["기말재고"]))}
                key = str(lineno)  # Preserve source rows, including zero quantities and identical records.
            elif kind == "mapping":
                if not d["아마란스 품번"] or not d["ICUBE 품번"]:
                    raise InputError("두 품번을 모두 입력해 주세요.")
                payload = {"erp": d["아마란스 품번"], "icube": d["ICUBE 품번"].upper()}
                key = payload["erp"]
            elif kind == "rules":
                factory = d.get("공장") or "1·2"
                factory = {"1,2": "1·2", "1/2": "1·2", "1,2공장": "1·2", "1": "1·2", "2": "1·2", "3공장": "3"}.get(factory, factory)
                if factory not in {"1·2", "3"}:
                    raise InputError("공장은 1·2 또는 3으로 입력해 주세요.")
                prefix, suffix = d["앞자리 KEY"].upper(), d["끝자리 KEY"].upper()
                if not re.fullmatch(r"[A-Z0-9]{4}", prefix):
                    raise InputError("앞자리 KEY는 영문·숫자 4자리입니다.")
                if factory == "3" and not suffix:
                    suffix = "*"
                if not re.fullmatch(r"[A-Z0-9]{2}", suffix) and not (factory == "3" and suffix == "*"):
                    raise InputError("끝자리 KEY는 영문·숫자 2자리입니다. 3공장 공통 규칙만 * 또는 빈칸을 사용하세요.")
                days = number(d["유효일수"]) if d.get("유효일수") else number(d.get("유효기간")) * 365
                if days != int(days) or not 1 <= days <= 36500:
                    raise InputError("유효일수는 1~36,500의 정수로 입력해 주세요.")
                product = d.get("MTS 제품구분", "").upper()
                if factory == "3" and not product:
                    raise InputError("3공장은 MTS 자료와 연결할 제품구분을 입력해 주세요.")
                payload = {"factory": factory, "prefix": prefix, "suffix": suffix, "days": int(days), "product": product}
                key = prefix + "|" + suffix
            elif kind == "mts":
                if not d["제품명"]:
                    raise InputError("같은 LOT의 CP·HD를 구분할 제품명 또는 제품구분이 필요합니다.")
                payload = {"product": d["제품명"].upper(), "lot": mts_lot(d["제조번호"]), "expiry": iso_date(d["사용기한"]).isoformat()}
                key = payload["product"] + "|" + payload["lot"]
            else:
                if not d["아마란스 품번"] or not d["LOT No."] or not d["사유"]:
                    raise InputError("예외는 품번·LOT·사유를 모두 입력해 주세요.")
                payload = {"erp": d["아마란스 품번"], "lot": d["LOT No."].upper(), "expiry": iso_date(d["사용기한"]).isoformat(), "reason": d["사유"]}
                key = payload["erp"] + "|" + payload["lot"]
            if any(len(str(v)) > 500 for v in payload.values()):
                raise InputError("한 셀의 내용은 500자 이내로 입력해 주세요.")
            if key in seen:
                if seen[key] != payload:
                    raise InputError("동일한 KEY에 서로 다른 값이 있습니다. 충돌을 정리한 뒤 등록해 주세요.")
                notes["동일 중복 통합"] += 1
                continue
            seen[key] = payload
            entries.append({"key": key, "data": payload})
        except InputError as exc:
            raise InputError(f"{lineno}행: {exc}") from exc
        if len(entries) > 50000:
            raise InputError("한 번에 50,000개 KEY까지 등록할 수 있습니다.")
    if not entries:
        raise InputError("등록할 데이터가 없습니다.")
    if kind == "stock" and reported_total is not None:
        actual = sum((number(e["data"]["quantity"]) for e in entries), Decimal(0))
        if actual != reported_total:
            raise InputError(f"기말재고 합계가 일치하지 않습니다. 원본 합계 {reported_total}, 읽은 합계 {actual}")
        notes["기말재고 합계 일치"] = 1
    if kind == "stock":
        notes.update(stock_scope(entries)[1])
    return entries, dict(notes)


def index_mts(refs):
    by_lot = {}
    for entry in refs.get('mts', {}).values():
        by_lot.setdefault(entry['lot'], []).append(entry)
    return dict(refs, mts_by_lot=by_lot)


def normalized_product(value):
    value = re.sub(r"[\s_-]+", " ", clean(value).upper())
    # Only the equivalence confirmed by the owner is automatic; other brands use explicit aliases.
    return re.sub(r"(?<![A-Z0-9])(?:HAHA\s*GEN|S\s*GEN|하하겐)(?![A-Z0-9])", "S GEN", value)


def has_tibialis(value):
    return bool(re.search(r"(?<![A-Z0-9])TIBIALIS(?![A-Z0-9])", normalized_product(value)))


def product_matches(product, key):
    product = normalized_product(product)
    for alias in key.split("|"):
        alias = normalized_product(alias)
        if not alias:
            continue
        if "S GEN" in alias:
            # Plain and injectable products must never match each other's MTS dates.
            inject = r"(?<![A-Z0-9])INJECT(?![A-Z0-9])"
            if bool(re.search(inject, product)) != bool(re.search(inject, alias)):
                continue
        if re.search(r"(?<![A-Z0-9])" + re.escape(alias) + r"(?![A-Z0-9])", product):
            return True
    return False


def calculate(row, refs, as_of, thresholds=(90, 180, 365)):
    result = dict(row, icube="", expiry=None, remaining=None, status="확인 필요", error="", source="", factory="")
    def fail(message):
        result["error"] = message
        return result
    if not is_finished_product(row):
        result.update(status="집계 제외", source="계정구분이 제품인 재고만 집계합니다.")
        return result
    if not row["lot"]:
        return fail("LOT 없음")
    mapping = refs.get("mapping", {}).get(row["erp"])
    if not mapping or mapping["icube"] in {"미관리", "미관", "N/A", "#N/A"}:
        return fail("품번 미매핑")
    icube = mapping["icube"]
    result["icube"] = icube
    rules = refs.get("rules", {})
    exact, family = rules.get(icube[:4] + "|" + icube[-2:]), rules.get(icube[:4] + "|*")
    if exact and family and (exact["factory"] != "3" or family["factory"] != "3"):
        return fail("공장 규칙 충돌")
    rule = exact or family
    # The owner explicitly groups all 41-series Tibialis tendons, regardless of anterior/posterior.
    tibialis = icube.startswith("41") and (has_tibialis(row["name"]) or (rule and has_tibialis(rule.get("product", ""))))
    if tibialis:
        rule = {"factory": "3", "product": "TIBIALIS"}
    exception = refs.get("exceptions", {}).get(row["erp"] + "|" + row["lot"])
    if exception:
        expiry = iso_date(exception["expiry"])
        result["source"] = "LOT 예외: " + exception["reason"]
        result["factory"] = rule["factory"] if rule else "예외"
    elif rule and rule["factory"] == "1·2":
        result["factory"] = "1·2"
        legacy_xbp = bool(re.fullmatch(r"XBP\d{6}[A-Z0-9]+", row["lot"]))
        if not legacy_xbp and not re.fullmatch(r"[A-Z]{2}\d{6}[A-Z0-9]+", row["lot"]):
            return fail("생산일 LOT 형식 확인 필요")
        raw = row["lot"][3:9] if legacy_xbp else row["lot"][2:8]
        try:
            manufactured = date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:]))
        except ValueError:
            return fail("생산일 오류")
        expiry = manufactured + timedelta(days=rule["days"] - 1)
        result["source"] = f"생산일 {manufactured} + {rule['days']}일 − 1일"
        if legacy_xbp:
            result["source"] += " (기존 XBP 예외: 4~9자리 생산일)"
    elif rule and rule["factory"] == "3":
        result["factory"] = "3"
        try:
            lot = mts_lot(row["lot"])
        except InputError:
            return fail("3공장 LOT 형식 확인 필요 · 예외 여부 확인")
        candidates = refs.get('mts_by_lot', {}).get(lot)
        if candidates is None:
            candidates = [v for v in refs.get('mts', {}).values() if v.get('lot',lot) == lot]
        matches = [v for v in candidates if product_matches(v.get('product', ''), rule['product'])]
        exact_mts = refs.get('mts', {}).get(rule['product'] + '|' + lot)
        if exact_mts and exact_mts not in matches:
            matches.append(exact_mts)
        if not matches:
            return fail("MTS 미매핑: " + rule["product"] + " + " + lot)
        if len({v['expiry'] for v in matches}) != 1:
            return fail("MTS 사용기한 충돌: " + rule['product'] + " + " + lot)
        expiry = iso_date(matches[0]["expiry"])
        result["source"] = "MTS " + rule["product"] + " + " + lot
    else:
        return fail("규칙 미등록: " + icube[:4] + " / " + icube[-2:])
    days = (expiry - as_of).days
    result.update(expiry=expiry.isoformat(), remaining=days)
    result["status"] = "만료" if days < 0 else "오늘 만료" if days == 0 else next((f"{n}일 미만" for n in thresholds if days < n), f"{thresholds[-1]}일 이상")
    return result


def summarize(rows):
    counts = Counter()
    quantities = {}
    for row in rows:
        if not is_finished_product(row):
            continue
        qty = number(row["quantity"])
        if qty == 0:
            continue
        counts[row["status"]] += 1
        key = row["status"] + " / " + row["unit"]
        quantities[key] = str(number(quantities.get(key)) + qty)
    return {"counts": dict(counts), "quantities": quantities}
