"""Presentation-only expiry grouping. Original LOT/date calculation is unchanged."""
from decimal import Decimal
from flask import render_template, request, url_for
from inventory_spec_view import clean, display, natural, factory_for, FACTORIES
from product_display_master import lookup

BANDS = [('expired', '만료·오늘 만료'), ('due90', '1~90일'), ('due180', '91~180일'),
         ('due365', '181~365일'), ('safe', '365일 초과'), ('issue', '확인 필요')]

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

def build_location_summary(rows, refs, as_of, factory=''):
    """Aggregate LOT rows into warehouse/location/product/spec management rows."""
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
        section = sections.setdefault(fac, dict(key=fac, label=FACTORIES[fac], places={}))
        place = section['places'].setdefault((warehouse, location), dict(
            warehouse=warehouse, location=location, rows={}, source_rows=[]))
        place['source_rows'].append(row)
        item = place['rows'].setdefault((name, size), dict(
            name=name, size=size, source_rows=[], amount=Decimal('0'), missing_cost=False,
            earliest=None, earliest_remaining=None, earliest_band='issue'))
        item['source_rows'].append(row)
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
        places = []
        for place in section['places'].values():
            items = list(place['rows'].values())
            for item in items:
                item['totals'] = unit_totals(item.pop('source_rows'))
            items.sort(key=lambda item: (natural(item['name']), natural(item['size'])))
            place['rows'] = items
            place['totals'] = unit_totals(place.pop('source_rows'))
            place['amount'] = sum((item['amount'] for item in items), Decimal('0'))
            place['missing_cost'] = any(item['missing_cost'] for item in items)
            places.append(place)
        places.sort(key=lambda place: (natural(place['warehouse']), natural(place['location'])))
        section['places'] = places
        section['totals'] = [dict(unit=unit, quantity=sum(
            (total['quantity'] for place in places for total in place['totals'] if total['unit'] == unit), Decimal('0')))
            for unit in sorted({total['unit'] for place in places for total in place['totals']})]
        section['amount'] = sum((place['amount'] for place in places), Decimal('0'))
        section['missing_cost'] = any(place['missing_cost'] for place in places)
        ordered.append(section)
    return dict(sections=ordered, row_count=sum(len(place['rows']) for section in ordered for place in section['places']))

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
    detail_mode = request.args.get('view') == 'detail'
    if request.args.get('columns_set') == '1':
        selected_columns = {value for value in request.args.getlist('column') if value in {'amount', 'expiry'}}
    else:
        selected_columns = {'amount', 'expiry'}
    all_board = build_expiry_board(rows, refs, selected_factory)
    if selected_bands:
        rows = [row for row in rows if expiry_band(row) in selected_bands]
    board = build_expiry_board(rows, refs, selected_factory)
    location_board = build_location_summary(rows, refs, as_of, selected_factory)
    def filter_url(**changes):
        params = request.args.to_dict(flat=False)
        params.pop('page',None)
        params.update(changes)
        return url_for(request.endpoint, **params)
    return render_template('expiry_display.html', board=board, location_board=location_board,
        detail_mode=detail_mode,selected_columns=selected_columns,snapshot=snapshot, as_of=as_of,
        warehouses=warehouses,locations=locations,statuses=statuses,bands=BANDS,band_stats=all_board['stats'],
        selected_warehouses=selected_warehouses,selected_locations=selected_locations,
        selected_bands=selected_bands,factories=FACTORIES,
        selected_factory=selected_factory,filter_url=filter_url,scope=summary.get('scope',{}),
        export_url=url_for('expiry.export', **request.args.to_dict(flat=False)))
