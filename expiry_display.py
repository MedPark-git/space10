"""Presentation-only expiry grouping. Original LOT/date calculation is unchanged."""
from decimal import Decimal
from flask import render_template, request, url_for
from inventory_spec_view import clean, display, natural, factory_for, product_family, FACTORIES
from product_display_master import lookup

BANDS = [('expired', '만료'), ('due90', '3개월 미만'), ('due180', '6개월 미만'),
         ('due365', '1년 미만'), ('safe', '1년 초과'), ('issue', '확인 필요')]

def expiry_band(row):
    days = row.get('remaining')
    if row.get('error') or days is None:
        return 'issue'
    for key, end in [('expired', 0), ('due90', 90), ('due180', 180), ('due365', 365)]:
        if days <= end:
            return key
    return 'safe'

def unit_totals(rows):
    """Keep unlike units separate in every displayed subtotal."""
    result = {}
    for row in rows:
        unit = clean(row.get('unit')).upper() or '단위 미등록'
        result[unit] = result.get(unit, Decimal('0')) + Decimal(str(row.get('quantity') or 0))
    return [dict(unit=unit, quantity=quantity) for unit, quantity in sorted(result.items())]

def cost_index(refs):
    result = {}
    for payload in refs.get('unit_cost', {}).values():
        if not isinstance(payload, dict):
            continue
        icube = clean(payload.get('icube')).upper()
        month = clean(payload.get('month'))
        try:
            cost = Decimal(str(payload.get('cost') or 0))
        except Exception:
            continue
        if icube and len(month) == 7 and cost >= 0:
            result.setdefault(icube, []).append((month, cost))
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result

def effective_cost(indexed, icube, as_of):
    target = as_of.strftime('%Y-%m')
    candidates = [item for item in indexed.get(clean(icube).upper(), []) if item[0] <= target]
    return candidates[-1] if candidates else (None, None)

def build_location_summary(rows, refs, as_of, factory='', group_by='warehouse'):
    """Aggregate by expiry risk first, then the selected warehouse/location axis."""
    indexed_costs = cost_index(refs)
    sections = {}
    for source in rows:
        row = dict(source)
        shown = lookup(row.get('icube'), row.get('name'), row.get('spec'), refs=refs)
        name = display(shown.get('name'))
        size = display(shown.get('size')) or '-'
        fac = factory_for(clean(row.get('icube')), name, row, refs)
        if factory and fac != factory:
            continue
        warehouse = clean(row.get('warehouse')) or '창고 미지정'
        location = clean(row.get('location')) or '장소 미지정'
        band_key = expiry_band(row)
        band_label = dict(BANDS)[band_key]
        if group_by == 'location':
            axis_key, axis_label = location, location
        elif group_by == 'both':
            axis_key, axis_label = (warehouse, location), f'{warehouse} → {location}'
        else:
            axis_key, axis_label = warehouse, warehouse
        section = sections.setdefault(fac, dict(key=fac, label=FACTORIES[fac], bands={}, source_rows=[]))
        section['source_rows'].append(row)
        band = section['bands'].setdefault(band_key, dict(
            key=band_key, label=band_label, groups={}, source_rows=[]))
        band['source_rows'].append(row)
        group = band['groups'].setdefault(axis_key, dict(label=axis_label, families={}, source_rows=[]))
        group['source_rows'].append(row)
        family_name = product_family(name)
        family = group['families'].setdefault(family_name, dict(label=family_name, rows={}, source_rows=[]))
        family['source_rows'].append(row)
        item = family['rows'].setdefault((name, size), dict(
            name=name, size=size, source_rows=[], amount=Decimal('0'), missing_cost=False,
            earliest=None, earliest_remaining=None, earliest_band='issue',
            warehouses=set(), locations=set(), lots=set()))
        item['source_rows'].append(row)
        item['warehouses'].add(warehouse)
        item['locations'].add(location)
        if clean(row.get('lot')):
            item['lots'].add(clean(row.get('lot')))
        quantity = Decimal(str(row.get('quantity') or 0))
        if clean(row.get('unit')).upper() == 'EA' and quantity != 0:
            _, cost = effective_cost(indexed_costs, row.get('icube'), as_of)
            if cost is None:
                item['missing_cost'] = True
            else:
                item['amount'] += quantity * cost
        expiry = row.get('expiry')
        if expiry and not row.get('error') and (item['earliest'] is None or expiry < item['earliest']):
            item['earliest'] = expiry
            item['earliest_remaining'] = row.get('remaining')
            item['earliest_band'] = expiry_band(row)
    ordered = []
    for fac in FACTORIES:
        if fac not in sections:
            continue
        section = sections[fac]
        bands = []
        for band_key, band_label in BANDS:
            if band_key not in section['bands']:
                continue
            band = section['bands'][band_key]
            groups = []
            for group in band['groups'].values():
                families = []
                for family in group['families'].values():
                    items = list(family['rows'].values())
                    for item in items:
                        item['totals'] = unit_totals(item.pop('source_rows'))
                        item['warehouse_count'] = len(item.pop('warehouses'))
                        item['location_count'] = len(item.pop('locations'))
                        item['lot_count'] = len(item.pop('lots'))
                        if group_by == 'warehouse' and item['location_count'] > 1:
                            item['spread_warning'] = f"{item['location_count']}개 장소 분산"
                        elif group_by == 'location' and item['warehouse_count'] > 1:
                            item['spread_warning'] = f"{item['warehouse_count']}개 창고 분산"
                        else:
                            item['spread_warning'] = ''
                    items.sort(key=lambda item: (natural(item['name']), natural(item['size'])))
                    family['rows'] = items
                    family['totals'] = unit_totals(family.pop('source_rows'))
                    family['amount'] = sum((item['amount'] for item in items), Decimal('0'))
                    family['missing_cost'] = any(item['missing_cost'] for item in items)
                    family['spread_count'] = sum(bool(item['spread_warning']) for item in items)
                    families.append(family)
                family_rank = {'XB 계열': 0, 'XP 계열': 1, 'ALLO 계열': 2, 'OSS 계열': 3,
                               'DBM 계열': 4, 'S DERM 계열': 5, 'S GEN 계열': 6}
                families.sort(key=lambda family: (family_rank.get(family['label'], 99), natural(family['label'])))
                group['families'] = families
                group['totals'] = unit_totals(group.pop('source_rows'))
                group['amount'] = sum((family['amount'] for family in families), Decimal('0'))
                group['missing_cost'] = any(family['missing_cost'] for family in families)
                group['spread_count'] = sum(family['spread_count'] for family in families)
                groups.append(group)
            groups.sort(key=lambda group: natural(group['label']))
            band['groups'] = groups
            band['totals'] = unit_totals(band.pop('source_rows'))
            band['amount'] = sum((group['amount'] for group in groups), Decimal('0'))
            band['missing_cost'] = any(group['missing_cost'] for group in groups)
            band['item_count'] = sum(len(family['rows']) for group in groups for family in group['families'])
            band['spread_count'] = sum(group['spread_count'] for group in groups)
            bands.append(band)
        section['bands'] = bands
        section['totals'] = unit_totals(section.pop('source_rows'))
        section['amount'] = sum((band['amount'] for band in bands), Decimal('0'))
        section['missing_cost'] = any(band['missing_cost'] for band in bands)
        ordered.append(section)
    risk_totals = {key: dict(label=label, quantity=Decimal('0'), amount=Decimal('0'),
                             item_count=0, missing_cost=False, spread_count=0)
                   for key, label in BANDS}
    for section in ordered:
        for band in section['bands']:
            risk = risk_totals[band['key']]
            risk['quantity'] += sum((total['quantity'] for total in band['totals'] if total['unit'] == 'EA'), Decimal('0'))
            risk['amount'] += band['amount']
            risk['item_count'] += band['item_count']
            risk['missing_cost'] = risk['missing_cost'] or band['missing_cost']
            risk['spread_count'] += band['spread_count']
    return dict(sections=ordered, risk_totals=risk_totals, row_count=sum(
        len(family['rows']) for section in ordered for band in section['bands']
        for group in band['groups'] for family in group['families']))

def build_expiry_board(rows, refs, factory=''):
    sections = {}
    stats = {key: dict(quantity=Decimal('0'), lots=set()) for key, _ in BANDS}
    row_count = 0
    excluded_count = 0
    for source in rows:
        row = dict(source)
        shown = lookup(row.get('icube'), row.get('name'), row.get('spec'), refs=refs)
        name, typ, size, category = [display(shown.get(k)) for k in ('name','type','size','category')]
        fac = factory_for(clean(row.get('icube')), name, row, refs)
        if factory and fac != factory:
            continue
        row_count += 1
        row.update(display_name=name, display_type=typ, display_size=size or '-', display_category=category,
                   display_factory=fac, band=expiry_band(row))
        quantity = Decimal(str(row.get('quantity') or 0))
        row['quantity'] = quantity
        ea = clean(row.get('unit')).upper() == 'EA' and quantity > 0
        if not ea:
            excluded_count += 1
        else:
            stats[row['band']]['quantity'] += quantity
            stats[row['band']]['lots'].add((row.get('erp'), row.get('lot') or ('missing', row_count)))
        section = sections.setdefault(fac, dict(key=fac,label=FACTORIES[fac],products={}))
        product = section['products'].setdefault(name, dict(name=name,rows=[],groups={},quantity=Decimal('0')))
        product['rows'].append(row)
        if ea:
            product['quantity'] += quantity
        group = product['groups'].setdefault((category,typ), dict(category=category,type=typ,rows=[]))
        group['rows'].append(row)
    ordered = []
    order = refs.get('product_order',{}).get('dashboard',{})
    for fac in FACTORIES:
        if fac not in sections:
            continue
        section = sections[fac]
        names = order.get('factory3' if fac == '3' else 'factory12', []) if isinstance(order, dict) else []
        rank = {name:i for i,name in enumerate(names)} if isinstance(names,list) else {}
        products = list(section['products'].values())
        products.sort(key=lambda p:(rank.get(p['name'],10**9),natural(p['name'])))
        for product in products:
            groups = list(product['groups'].values())
            groups.sort(key=lambda g:({'국내':0,'CE':1,'일반수출':2}.get(g['category'],3),natural(g['category']),natural(g['type'])))
            for group in groups:
                group['rows'].sort(key=lambda r:(natural(r['display_size']), r.get('expiry') or '9999', clean(r.get('lot'))))
                group['totals'] = unit_totals(group['rows'])
            product['groups'] = groups
            product['totals'] = unit_totals(product['rows'])
        section['products'] = products
        section['totals'] = unit_totals([row for product in products for row in product['rows']])
        ordered.append(section)
    return dict(sections=ordered,stats=stats,total=sum(s['quantity'] for s in stats.values()),
                row_count=row_count,excluded_count=excluded_count,
                product_count=sum(len(s['products']) for s in ordered))

def render_expiry_display(current_view):
    refs, snapshot, as_of, summary, warehouses, statuses, rows = current_view()
    locations = summary.get('filter_locations', [])
    selected_warehouses = [value for value in request.args.getlist('warehouse') if value]
    selected_location_tokens = [value for value in request.args.getlist('location') if value]
    selected_locations = [
        '' if value == '__unassigned__' else value
        for value in selected_location_tokens
    ]
    selected_factory = request.args.get('product_factory','')
    if selected_factory not in FACTORIES:
        selected_factory = ''
    valid_bands = {key for key, _ in BANDS}
    selected_bands = [value for value in request.args.getlist('expiry_band') if value in valid_bands]
    group_by = request.args.get('group_by', 'warehouse')
    if group_by not in {'warehouse', 'location', 'both'}:
        group_by = 'warehouse'
    detail_mode = request.args.get('view') == 'detail'
    if request.args.get('columns_set') == '1':
        selected_columns = {value for value in request.args.getlist('column') if value in {'amount', 'expiry'}}
    else:
        selected_columns = {'amount'}
    all_board = build_expiry_board(rows, refs, selected_factory)
    all_location_board = build_location_summary(rows, refs, as_of, selected_factory, group_by)
    if selected_bands:
        rows = [row for row in rows if expiry_band(row) in selected_bands]
    board = build_expiry_board(rows, refs, selected_factory)
    location_board = build_location_summary(rows, refs, as_of, selected_factory, group_by)
    def filter_url(**changes):
        params = request.args.to_dict(flat=False)
        params.pop('page',None)
        params.update(changes)
        return url_for(request.endpoint, **params)
    return render_template('expiry_display.html', board=board, location_board=location_board,
        detail_mode=detail_mode,selected_columns=selected_columns,group_by=group_by,snapshot=snapshot, as_of=as_of,
        warehouses=warehouses,locations=locations,statuses=statuses,bands=BANDS,band_stats=all_board['stats'],
        risk_stats=all_location_board['risk_totals'],
        selected_warehouses=selected_warehouses,selected_locations=selected_locations,
        selected_bands=selected_bands,factories=FACTORIES,
        selected_factory=selected_factory,filter_url=filter_url,scope=summary.get('scope',{}),
        export_url=url_for('expiry.export', **request.args.to_dict(flat=False)))
