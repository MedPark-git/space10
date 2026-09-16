from datetime import date
import pytest

from expiry_engine import InputError, calculate, parse_paste, summarize, stock_scope, product_matches, family_rules, parse_family_rule


def stock(erp="0001", lot="XB240229C1001", name="BONE XB"):
    return dict(erp=erp, lot=lot, name=name, account="제품", warehouse="완제품", location="보관실", unit="EA", quantity="2")


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


def test_factory_3_explicit_common_rules_preserve_cp_hd_separation():
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
    source="No\t창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\t재고단위\n1\t완제품\tA\t0001\t샘플\t제품\tXB240229C1\t1,200.25\tEA\n2\t완제품\tA\t0002\t샘플2\t제품\t\t\tEA\n\t합계\t\t\t\t\t\t1,200.25\t"
    entries,notes=parse_paste("stock",source)
    assert len(entries)==2 and entries[0]["data"]["erp"]=="0001"
    assert entries[1]["data"]["quantity"]=="0"
    assert notes["기말재고 합계 일치"]==1
    with pytest.raises(InputError,match="합계가 일치하지"):
        parse_paste("stock",source.replace("합계\t\t\t\t\t\t1,200.25","합계\t\t\t\t\t\t1,200.26"))


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
        parse_paste("stock",f"창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\nA\tB\t0001\tName\t제품\tLOT\t{qty}")


def test_only_finished_products_in_same_warehouse_are_counted_and_raw_total_still_checked():
    source = ('창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\t재고단위\n'
              '부적합창고\tA\t0001\t제품명\t제품\tXB240229C1\t2.25\tEA\n'
              '부적합창고\tA\t0002\t반제품명\t반제품\tBAD\t7.5\tEA\n'
              '부적합창고\tA\t0003\t미분류\t\t\t1\tEA\n'
              '합계\t\t\t\t\t\t10.75\t')
    entries, notes = parse_paste('stock', source)
    products, scope = stock_scope(entries)
    assert len(entries) == 3 and len(products) == 1
    assert scope == {'원본 행':3, '집계 대상 제품':1, '제품 외 제외':1, '계정구분 미확인 제외':1}
    assert notes['기말재고 합계 일치'] == 1
    rows = [calculate(e['data'], base_refs(), date(2026,9,16)) for e in entries]
    assert rows[1]['expiry'] is None and rows[1]['status'] == '집계 제외'
    assert sum(summarize(rows)['counts'].values()) == 1
    assert list(summarize(rows)['quantities'].values()) == ['2.25']
    with pytest.raises(InputError, match='합계가 일치하지'):
        parse_paste('stock', source.replace('10.75', '2.25'))


def test_account_header_is_required_and_missing_legacy_account_is_not_assumed_product():
    with pytest.raises(InputError, match='계정구분'):
        parse_paste('stock', '창고\t장소\t품번\t품명\tLOT No.\t기말재고\nA\tB\t1\t품명\tLOT\t1')
    row = stock(); row.pop('account')
    assert calculate(row, base_refs(), date(2026,9,16))['status'] == '집계 제외'


@pytest.mark.parametrize('prefix', ['39FD', '42SP', '48SP'])
def test_factory_3_suffix_rules_are_preserved_and_override_common_rules(prefix):
    entries, _ = parse_paste('rules', f'공장\t앞자리 KEY\t끝자리 KEY\t유효일수\tMTS 제품구분\n3\t{prefix}\t01\t1095\tS GEN\n3\t{prefix}\t12\t1095\tHAHA GEN INJECT\n3\t{prefix}\t*\t1095\tOTHER')
    refs = {'mapping':{'0001':{'icube':prefix+'100-12'}}, 'rules':{e['key']:e['data'] for e in entries},
            'mts':{'A':{'product':'S GEN','lot':'240039SA','expiry':'2027-01-01'},
                   'B':{'product':'S GEN INJECT','lot':'240039SA','expiry':'2029-01-01'},
                   'C':{'product':'OTHER','lot':'240039SA','expiry':'2030-01-01'}}}
    assert set(refs['rules']) == {prefix+'|01', prefix+'|12', prefix+'|*'}
    row = stock(lot='240039SA', name='Original ODM name')
    result = calculate(row, refs, date(2026,9,16))
    assert result['expiry'] == '2029-01-01' and result['name'] == row['name']
    assert result['icube'] == prefix+'100-12'
    refs['mapping']['0001']['icube'] = prefix+'100-01'
    assert calculate(row, refs, date(2026,9,16))['expiry'] == '2027-01-01'
    refs['mapping']['0001']['icube'] = prefix+'100-99'
    assert calculate(row, refs, date(2026,9,16))['expiry'] == '2030-01-01'


@pytest.mark.parametrize('left,right', [('HAHA GEN','S GEN'), ('s-gen inject','haha gen inject'), ('하하겐','SGEN')])
def test_confirmed_brand_aliases_match_in_both_directions(left, right):
    assert product_matches(left, right) and product_matches(right, left)


def test_inject_plain_and_unrelated_products_are_not_mixed():
    assert not product_matches('HAHA GEN INJECT', 'S GEN')
    assert not product_matches('S GEN', 'HAHA GEN INJECT')
    assert not product_matches('SHD', 'HD')
    assert product_matches('ODM BRAND B', 'BRAND A | BRAND B')


@pytest.mark.parametrize('name', ['Tibialis Anterior tendon', 'TIBIALIS POSTERIOR TENDON'])
def test_41_tibialis_uses_common_mts_without_direction_and_conflicts_are_flagged(name):
    refs = {'mapping':{'0001':{'icube':'41TB100-12'}},
            'mts':{'A':{'product':'Tibialis Anterior tendon','lot':'240039SA','expiry':'2029-01-01'},
                   'B':{'product':'TIBIALIS POSTERIOR tendon','lot':'240039SA','expiry':'2029-01-01'}}}
    row = stock(lot='240039SA', name=name)
    assert calculate(row, refs, date(2026,9,16))['expiry'] == '2029-01-01'
    refs['mts']['B']['expiry'] = '2028-01-01'
    result = calculate(row, refs, date(2026,9,16))
    assert result['expiry'] is None and '충돌' in result['error']
    refs['mapping']['0001']['icube'] = '42TB100-12'
    assert '규칙 미등록' in calculate(row, refs, date(2026,9,16))['error']


@pytest.mark.parametrize('prefix', ['42AP', '44AP', '48AP', '49AP'])
@pytest.mark.parametrize('suffix', ['01', '12', 'CE', 'ZZ'])
def test_ap_1095_days_include_production_date_for_every_suffix(prefix, suffix):
    refs = {'mapping': {'0001': {'icube':prefix+'100-'+suffix}},
            'rules': {prefix+'|'+suffix: {'factory':'1·2', 'days':1825}}}
    result = calculate(stock(lot='SA240229P101'), refs, date(2026,9,16))
    assert result['expiry'] == '2027-02-27'
    assert '1095일' in result['source'] and '끝자리 무관' in result['source']


def test_ap_without_registered_rule_works_but_unknown_production_date_never_uses_mts():
    refs = {'mapping':{'0001':{'icube':'42AP100-NEW'}}}
    assert calculate(stock(lot='SA240229P101'), refs, date(2026,9,16))['expiry'] == '2027-02-27'
    refs['rules'] = {'42AP|*': {'factory':'3', 'days':1095, 'product':'AP'}}
    refs['mts'] = {'AP|240039SA': {'product':'AP', 'lot':'240039SA', 'expiry':'2028-01-01'}}
    result = calculate(stock(lot='240039SA'), refs, date(2026,9,16))
    assert result['expiry'] is None and '생산일 LOT 형식' in result['error']


def test_disabling_default_common_rule_restores_normal_rule_and_does_not_reactivate_default():
    refs = {'mapping':{'0001':{'icube':'42AP100-01'}},
            'rules':{'42AP|01':{'factory':'1·2','days':365}},
            'family_rules':{'42AP':{'prefix':'42AP','days':1095,'enabled':False,'reason':'검증 후 중지'}}}
    assert family_rules(refs)['42AP']['enabled'] is False
    assert calculate(stock(lot='SA240229P101'), refs, date(2026,9,16))['expiry'] == '2025-02-27'
    refs['rules'] = {}
    assert '규칙 미등록' in calculate(stock(lot='SA240229P101'), refs, date(2026,9,16))['error']


def test_specific_lot_exception_wins_over_common_period_and_normal_rule_conflicts():
    refs = {'mapping':{'0001':{'icube':'42AP100-01'}},
            'rules':{'42AP|01':{'factory':'1·2','days':365},'42AP|*':{'factory':'3','product':'AP','days':365}},
            'exceptions':{'0001|BAD':{'expiry':'2028-01-01','reason':'개별 확정'}}}
    assert calculate(stock(lot='BAD'), refs, date(2026,9,16))['expiry'] == '2028-01-01'
    refs['mapping']['0001']['icube'] = '11BC100-01'
    refs['rules'] = {'11BC|01':{'factory':'1·2','days':365},'11BC|*':{'factory':'3','days':365,'product':'OTHER'}}
    assert calculate(stock(lot='BAD'), refs, date(2026,9,16))['expiry'] == '2028-01-01'
    refs['exceptions'] = {}
    assert '충돌' in calculate(stock(), refs, date(2026,9,16))['error']


@pytest.mark.parametrize('change', [{'prefix':'42'}, {'days':'3.0'}, {'days':'0'}, {'days':'36501'}, {'reason':''}, {'enabled':'bad'}])
def test_family_rule_form_rejects_ambiguous_or_incomplete_input(change):
    with pytest.raises(InputError):
        parse_family_rule({'prefix':'42AP','days':'1095','enabled':'1','reason':'확정한 공통 규칙',**change})
