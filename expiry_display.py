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
    all_board = build_expiry_board(rows, refs, selected_factory)
    if selected_bands:
        rows = [row for row in rows if expiry_band(row) in selected_bands]
    board = build_expiry_board(rows, refs, selected_factory)
    def filter_url(**changes):
        params = request.args.to_dict(flat=False)
        params.pop('page',None)
        params.update(changes)
        return url_for(request.endpoint, **params)
    return render_template('expiry_display.html', board=board, snapshot=snapshot, as_of=as_of,
        warehouses=warehouses,locations=locations,statuses=statuses,bands=BANDS,band_stats=all_board['stats'],
        selected_warehouses=selected_warehouses,selected_locations=selected_locations,
        selected_bands=selected_bands,factories=FACTORIES,
        selected_factory=selected_factory,filter_url=filter_url,scope=summary.get('scope',{}),
        export_url=url_for('expiry.export', **request.args.to_dict(flat=False)))
