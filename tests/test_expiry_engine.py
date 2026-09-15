from datetime import date
import pytest

from expiry_engine import InputError, calculate, parse_paste, summarize


def stock(erp="0001", lot="XB240229C1001", name="BONE XB"):
    return dict(erp=erp, lot=lot, name=name, warehouse="완제품", location="보관실", unit="EA", quantity="2")


def base_refs():
    return {"mapping": {"0001": {"erp":"0001", "icube":"11BC025-01"}},
            "rules": {"11BC|01": {"prefix":"11BC", "suffix":"01", "factory":"1·2", "days":1095, "product":""}}}


def test_fixed_days_include_production_date_across_leap_year():
    result = calculate(stock(), base_refs(), date(2024, 2, 29))
    assert result["expiry"] == "2027-02-27"
    assert result["remaining"] == 1094


def test_expiry_day_and_next_day_are_distinct():
    assert calculate(stock(), base_refs(), date(2027,2,27))["status"] == "오늘 만료"
    assert calculate(stock(), base_refs(), date(2027,2,28))["status"] == "만료"


def test_invalid_lot_date_does_not_generate_expiry():
    r = calculate(stock(lot="XB230229C1001"), base_refs(), date(2026,9,15))
    assert r["expiry"] is None and r["error"] == "생산일 오류"


def test_unknown_suffix_does_not_fall_back_to_nearby_rule():
    refs=base_refs();refs["mapping"]["0001"]["icube"]="11BC025-TW"
    r=calculate(stock(),refs,date(2026,9,15))
    assert r["status"] == "확인 필요" and r["expiry"] is None


def test_mts_cannot_override_factory_12():
    refs=base_refs();refs["mts"]={"CP|240039SA":{"expiry":"2099-01-01"}}
    assert calculate(stock(),refs,date(2026,9,15))["expiry"]=="2027-02-27"


def test_same_lot_cp_hd_are_separate_and_suffix_ignored_for_factory_3():
    refs={"mapping":{"CP":{"icube":"40CP025-01"},"HD":{"icube":"40HD025-CE"}},
          "rules":{"40CP|*":{"factory":"3","product":"CP","days":1825},"40HD|*":{"factory":"3","product":"HD","days":730}},
          "mts":{"CP|240039SA":{"expiry":"2029-05-21"},"HD|240039SA":{"expiry":"2026-05-21"}}}
    assert calculate(stock("CP","240039SA"),refs,date(2026,9,15))["expiry"]=="2029-05-21"
    assert calculate(stock("HD","240039SA"),refs,date(2026,9,15))["expiry"]=="2026-05-21"
    refs["mapping"]["CP"]["icube"]="40CP025-ZV"
    assert calculate(stock("CP","240039SA"),refs,date(2026,9,15))["expiry"]=="2029-05-21"


def test_missing_mts_product_never_matches_other_product():
    refs={"mapping":{"CP":{"icube":"40CP025-01"}},"rules":{"40CP|*":{"factory":"3","product":"CP","days":1825}},
          "mts":{"HD|240039SA":{"expiry":"2026-05-21"}}}
    r=calculate(stock("CP","240039SA"),refs,date(2026,9,15))
    assert r["expiry"] is None and "MTS 미매핑" in r["error"]


def test_conflicting_mts_dates_rejected_and_identical_manufacturing_rows_collapsed():
    source="제품명\t제조번호\t사용기한\nCP\t240039SA0001\t2029-05-21\nCP\t240039SA0002\t2029-05-21"
    entries,notes=parse_paste("mts",source)
    assert len(entries)==1 and notes["동일 중복 통합"]==1
    with pytest.raises(InputError,match="충돌"):
        parse_paste("mts",source+"\nCP\t240039SA0003\t2028-05-21")


def test_mts_without_product_is_rejected():
    with pytest.raises(InputError,match="제품명"):
        parse_paste("mts","제조번호\t사용기한\n240039SA0001\t2029-05-21")


def test_excel_rules_import_accepts_leading_zero_suffix_and_duplicates():
    entries,notes=parse_paste("rules","KEY값\tKEY값2\t유효기간\n11BC\t01\t3\n11BC\t01\t3")
    assert entries[0]["key"]=="11BC|01" and entries[0]["data"]["days"]==1095
    assert notes["동일 중복 통합"]==1


def test_stock_zero_decimal_and_total_preserved():
    source="No\t창고\t장소\t품번\t품명\tLOT No.\t기말재고\t재고단위\n1\t완제품\tA\t0001\t샘플\tXB240229C1\t1,200.25\tEA\n2\t완제품\tA\t0002\t샘플2\t\t\tEA\n\t합계\t\t\t\t\t1,200.25\t"
    entries,notes=parse_paste("stock",source)
    assert len(entries)==2 and entries[0]["data"]["erp"]=="0001"
    assert entries[1]["data"]["quantity"]=="0"
    assert notes["기말재고 합계 일치"]==1
    with pytest.raises(InputError,match="합계가 일치하지"):
        parse_paste("stock",source.replace("합계\t\t\t\t\t1,200.25","합계\t\t\t\t\t1,200.26"))


def test_unit_totals_not_combined():
    rows=[dict(stock(),status="만료"),dict(stock(),status="만료",unit="g",quantity="0.25")]
    s=summarize(rows)
    assert s["quantities"]=={"만료 / EA":"2", "만료 / g":"0.25"}


def test_exception_is_exact_product_and_lot_only():
    refs=base_refs();refs["exceptions"]={"0001|XBP-BAD":{"expiry":"2028-01-01","reason":"확인된 예외"}}
    assert calculate(stock(lot="XBP-BAD"),refs,date(2026,9,15))["expiry"]=="2028-01-01"
    assert calculate(stock(lot="XBP-OTHER"),refs,date(2026,9,15))["expiry"] is None


def test_legacy_xbp_uses_fourth_character_date_as_in_source_workbook():
    refs=base_refs();refs['rules']['11BC|01']['days']=1825
    assert calculate(stock(lot='XBP191024C4014'),refs,date(2026,9,15))['expiry']=='2024-10-21'


def test_mts_product_phrase_matching_preserves_group_and_detects_conflicts():
    refs={'mapping':{'CP':{'icube':'40CP025-01'}},'rules':{'40CP|*':{'factory':'3','product':'CP','days':1825}},
          'mts':{'A1 CP|240039SA':{'product':'A1 CP','lot':'240039SA','expiry':'2029-05-21'},
                 'A1 HD|240039SA':{'product':'A1 HD','lot':'240039SA','expiry':'2026-05-21'}}}
    assert calculate(stock('CP','240039SA'),refs,date(2026,9,15))['expiry']=='2029-05-21'
    refs['mts']['A2 CP|240039SA']={'product':'A2 CP','lot':'240039SA','expiry':'2028-05-21'}
    assert '충돌' in calculate(stock('CP','240039SA'),refs,date(2026,9,15))['error']


@pytest.mark.parametrize("qty",["NaN","Infinity","-Infinity","1000000000000","0.0000001"])
def test_invalid_quantities_rejected(qty):
    with pytest.raises(InputError):
        parse_paste("stock",f"창고\t장소\t품번\t품명\tLOT No.\t기말재고\nA\tB\t0001\tName\tLOT\t{qty}")
