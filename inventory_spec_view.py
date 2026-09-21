import re
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import abort, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select

from product_display_master import lookup as product_lookup

ZERO = Decimal('0')
FACTORIES = {'12': '1·2공장', '3': '3공장', 'unknown': '공장 확인 필요'}
OVERRIDES = {'MedParkAlloD': '3', 'S1-Allo 덴탈': '3'}


def clean(value):
    return str(value if value is not None else '').strip().lstrip('\ufeff')


def num(value):
    raw = clean(value).replace(',', '')
    try:
        out = Decimal(raw or '0')
    except InvalidOperation as exc:
        raise ValueError('invalid number') from exc
    if not out.is_finite():
        raise ValueError('invalid number')
    return out


def natural(value):
    result = []
    for part in re.split(r'(\d+(?:\.\d+)?)', clean(value).casefold()):
        if not part:
            continue
        try:
            result.append((0, Decimal(part)))
        except InvalidOperation:
            result.append((1, part))
    return tuple(result)


def display(value):
    return re.sub(r'\s+', ' ', re.sub(r'\\([~*])', r'\1', clean(value))).replace('*', '×')


def place_key(value):
    return re.sub(r'[\s_]+', '', clean(value)).casefold()


def qty_filter(value):
    out = format(num(value), ',f')
    return out.rstrip('0').rstrip('.') if '.' in out else out


def won_filter(value):
    return format(num(value), ',.0f')


def totals():
    return dict(total=ZERO, available=ZERO, other=ZERO, unusable=ZERO,
                available_finished=ZERO, available_work=ZERO, available_elsewhere=ZERO,
                location_exception=ZERO, value=ZERO, available_value=ZERO,
                other_value=ZERO, unusable_value=ZERO, missing_quantity=ZERO,
                costed_quantity=ZERO, available_missing=ZERO, other_missing=ZERO,
                unusable_missing=ZERO)


def add_total(target, row):
    quantity = row['quantity']
    bucket = row['bucket']
    target['total'] += quantity
    target[bucket] += quantity
    if bucket == 'available':
        if row['physical'].startswith('finished'):
            target['available_finished'] += quantity
        elif row['physical'].startswith('work'):
            target['available_work'] += quantity
        else:
            target['available_elsewhere'] += quantity
    if row['location_exception']:
        target['location_exception'] += quantity
    if row['cost'] is None:
        target['missing_quantity'] += quantity
        target[bucket + '_missing'] += quantity
    else:
        value = quantity * row['cost']
        target['costed_quantity'] += quantity
        target['value'] += value
        target[bucket + '_value'] += value


def factory_for(code, name, raw, refs):
    if name in OVERRIDES:
        return '3'
    rule = refs.get('rules', {}).get(code[:4] + '|' + code[-2:]) or refs.get('rules', {}).get(code[:4] + '|*') or {}
    if not isinstance(rule, dict):
        rule = {}
    if code.startswith('41') and re.search(r'\bTIBIALIS\b', (clean(raw.get('name')) + ' ' + clean(rule.get('product'))).upper()):
        return '3'
    source = re.sub(r'[\s·,/_]+', '', clean(rule.get('factory') or raw.get('factory'))).replace('공장', '')
    return '12' if source in {'1', '2', '12'} else '3' if source == '3' else 'unknown'


def stock_bucket(raw, factory, refs):
    warehouse = place_key(raw.get('warehouse'))
    location = place_key(raw.get('location'))
    classes = refs.get('warehouse_classes', {})
    saved = classes.get(warehouse)
    if not isinstance(saved, dict):
        saved = None
        for key, payload in classes.items():
            if place_key(key) == warehouse and isinstance(payload, dict):
                saved = payload
                break
    state = (saved or {}).get('classification')
    if warehouse == place_key('완제품창고'):
        physical = 'finished12'
    elif warehouse == place_key('3공장 완제품창고'):
        physical = 'finished3'
    elif '공정중' in warehouse:
        if '3공장' in location:
            physical = 'work3'
        elif any(token in location for token in ('1공장', '2공장', '1·2공장')):
            physical = 'work12'
        else:
            physical = 'work_unknown'
    else:
        physical = 'other'
    if state not in {'available', 'unavailable', 'unclassified'}:
        state = 'available' if physical.startswith('finished') else 'unclassified'
    exception = factory == '3' and physical in {'finished12', 'work12'}
    bucket = 'unusable' if state == 'unavailable' else 'other' if exception else 'available' if state == 'available' else 'other'
    return bucket, physical, exception


def cost_index(refs, target_month):
    result = {}
    for payload in refs.get('unit_cost', {}).values():
        if not isinstance(payload, dict):
            continue
        code = clean(payload.get('icube')).upper()
        month = clean(payload.get('month'))
        if not code or not re.fullmatch(r'\d{4}-\d{2}', month) or month > target_month:
            continue
        try:
            cost = num(payload.get('cost'))
        except ValueError:
            continue
        if cost < 0:
            continue
        if code not in result or month > result[code][0]:
            result[code] = (month, cost)
    return result


def category_key(value):
    return ({'국내': 0, '일반수출': 1}.get(value, 2), natural(value))


def build_board(entries, refs, filters, snapshot_date):
    selected = dict(q=clean(filters.get('q')), warehouses=list(filters.get('warehouses') or []),
                    product_factory=clean(filters.get('product_factory')),
                    availability=clean(filters.get('availability')),
                    show_value=bool(filters.get('show_value')))
    if selected['product_factory'] not in FACTORIES:
        selected['product_factory'] = ''
    selected['availability'] = {'unavailable': 'unusable', 'unclassified': 'other'}.get(selected['availability'], selected['availability'])
    if selected['availability'] not in {'available', 'other', 'unusable'}:
        selected['availability'] = ''
    costs = cost_index(refs, snapshot_date.strftime('%Y-%m'))
    source_rows = []
    invalid_rows = 0
    excluded_rows = 0
    mapping = refs.get('mapping', {})
    for entry in entries:
        raw = entry.get('data', {}) if isinstance(entry, dict) else {}
        if not isinstance(raw, dict) or clean(raw.get('account')) != '제품':
            excluded_rows += 1
            continue
        try:
            quantity = num(raw.get('quantity'))
        except ValueError:
            invalid_rows += 1
            continue
        if quantity <= 0 or clean(raw.get('unit')).upper() != 'EA':
            excluded_rows += 1
            continue
        erp = clean(raw.get('erp'))
        mapped = mapping.get(erp) or {}
        code = clean(mapped.get('icube') or raw.get('icube')).upper()
        shown = product_lookup(code, raw.get('name'), raw.get('spec'))
        name = display(shown.get('name') or raw.get('name')) or '제품명 미등록'
        type_name = display(shown.get('type')) or '타입 미등록'
        size = display(shown.get('size'))
        original_spec = display(raw.get('spec'))
        tendon = name.casefold() == 'tendon'
        if tendon:
            size = '-'
        elif not size:
            size = original_spec or '규격 미등록'
        category = display(shown.get('category')) or '분류 확인 필요'
        factory = factory_for(code, name, raw, refs)
        bucket, physical, exception = stock_bucket(raw, factory, refs)
        cost_data = costs.get(code)
        source_rows.append(dict(name=name, type=type_name, size=size, category=category,
            original_spec=original_spec, erp=erp, icube=code, mapped=bool(shown.get('mapped')),
            factory=factory, quantity=quantity, warehouse=clean(raw.get('warehouse')) or '창고 미지정',
            place=clean(raw.get('location')) or '장소 미지정', bucket=bucket, physical=physical,
            location_exception=exception, cost=cost_data[1] if cost_data else None,
            cost_month=cost_data[0] if cost_data else None))
    warehouses = sorted({row['warehouse'] for row in source_rows}, key=natural)
    rows = []
    for row in source_rows:
        if selected['warehouses'] and row['warehouse'] not in selected['warehouses']:
            continue
        if selected['product_factory'] and row['factory'] != selected['product_factory']:
            continue
        if selected['availability'] and row['bucket'] != selected['availability']:
            continue
        if selected['q']:
            haystack = ' '.join(clean(row[key]) for key in ('name','type','size','category','erp','icube','original_spec','warehouse','place')).casefold()
            if selected['q'].casefold() not in haystack:
                continue
        rows.append(row)
    groups = {}
    missing_cost = set()
    unmapped = set()
    for row in rows:
        group = groups.setdefault((row['factory'], row['name']), dict(name=row['name'], factory=row['factory'],
            tendon=row['name'].casefold() == 'tendon', lines={}, **totals()))
        add_total(group, row)
        line_key = (row['category'], row['type'], row['size'])
        if not row['mapped']:
            line_key += (row['icube'] or row['erp'],)
            unmapped.add(row['icube'] or row['erp'])
        line = group['lines'].setdefault(line_key, dict(category=row['category'], type=row['type'], size=row['size'],
            original_specs=set(), places={}, **totals()))
        add_total(line, row)
        if row['original_spec']:
            line['original_specs'].add(row['original_spec'])
        pkey = (row['warehouse'], row['place'], row['bucket'])
        line['places'][pkey] = line['places'].get(pkey, ZERO) + row['quantity']
        if row['cost'] is None and row['icube']:
            missing_cost.add(row['icube'])
    order = refs.get('product_order', {}).get('dashboard', {})
    if not isinstance(order, dict):
        order = {}
    sections = []
    overall = totals()
    for factory, factory_label in FACTORIES.items():
        saved = order.get('factory3' if factory == '3' else 'factory12', order.get('order', []))
        rank = {name: i for i, name in enumerate(saved if isinstance(saved, list) else [])}
        products = [group for (fac, _), group in groups.items() if fac == factory]
        products.sort(key=lambda group: (rank.get(group['name'], 10**9), natural(group['name'])))
        section_total = totals()
        for index, group in enumerate(products, 1):
            group['id'] = f'product-{factory}-{index}'
            lines = list(group.pop('lines').values())
            lines.sort(key=lambda row: (category_key(row['category']), natural(row['type']), natural(row['size'])))
            group['rows'] = lines
            group['specs'] = sorted({row['size'] for row in lines if row['size'] not in {'', '-'}}, key=natural)
            group['types'] = sorted({row['type'] for row in lines}, key=natural)
            spec_summary = {}
            for row in lines:
                label = row['type'] if group['tendon'] else row['size']
                summary = spec_summary.setdefault(label, dict(label=label, **totals()))
                for field in totals():
                    summary[field] += row[field]
            group['spec_summary'] = list(spec_summary.values())
            group['spec_summary'].sort(key=lambda item: natural(item['label']))
            categories = []
            for row in lines:
                row['original_specs'] = sorted(row.pop('original_specs'), key=natural)
                row['locations'] = [dict(warehouse=w, place=p, bucket=b, quantity=q)
                    for (w,p,b), q in sorted(row.pop('places').items())]
                if not categories or categories[-1]['name'] != row['category']:
                    categories.append(dict(name=row['category'], rows=[], **totals()))
                category = categories[-1]
                category['rows'].append(row)
                for field in totals():
                    category[field] += row[field]
            for category in categories:
                previous_type = None
                for row in category['rows']:
                    row['type_start'] = row['type'] != previous_type
                    previous_type = row['type']
            group['categories'] = categories
            for field in totals():
                section_total[field] += group[field]
                overall[field] += group[field]
        if products:
            sections.append(dict(key=factory, label=factory_label, products=products, **section_total))
    if overall['total'] != sum((row['quantity'] for row in rows), ZERO):
        raise ValueError('Inventory total mismatch')
    if overall['total'] != overall['available'] + overall['other'] + overall['unusable']:
        raise ValueError('Bucket total mismatch')
    for section in sections:
        for group in section['products']:
            if group['total'] != sum((row['total'] for row in group['rows']), ZERO):
                raise ValueError('Product total mismatch')
            if not all(row['size'] for row in group['rows']):
                raise ValueError('Missing specification label')
    return dict(filters=selected, sections=sections, metrics=overall, warehouses=warehouses,
                invalid_rows=invalid_rows, excluded_rows=excluded_rows, missing_cost=len(missing_cost),
                unmapped=len(unmapped), source_rows=len(rows), snapshot_date=snapshot_date,
                product_count=sum(len(section['products']) for section in sections))


def install_inventory_spec_view(app, db):
    models = app.extensions['expiry_models']
    Reference = models['Reference']
    Import = models['Import']
    app.jinja_env.filters['iv_qty'] = qty_filter
    app.jinja_env.filters['iv_won'] = won_filter

    @login_required
    def inventory_spec_dashboard():
        refs = {}
        for ref in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            refs.setdefault(ref.kind, {})[ref.key] = ref.payload
        stmt = select(Import).where(Import.kind == 'stock', Import.committed_at.is_not(None))
        selected_snapshot = clean(request.args.get('snapshot'))
        if selected_snapshot:
            snapshot = db.session.scalar(stmt.where(Import.id == selected_snapshot))
            if snapshot is None:
                abort(404)
        else:
            snapshot = db.session.scalar(stmt.order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))
        board = None
        if snapshot is not None:
            if not isinstance(snapshot.as_of, date):
                abort(400)
            filters = dict(q=request.args.get('q', ''), warehouses=request.args.getlist('warehouse'),
                product_factory=request.args.get('product_factory', ''), availability=request.args.get('availability', ''),
                show_value=request.args.get('show_value') == '1')
            board = build_board(snapshot.payload or [], refs, filters, snapshot.as_of)
        return render_template('inventory_specs.html', board=board, factories=FACTORIES,
                               can_edit=current_user.role in {'admin','editor'},
                               snapshot_id=snapshot.id if snapshot is not None else '')

    if 'expiry.dashboard' not in app.view_functions:
        raise RuntimeError('Existing inventory dashboard endpoint is missing')
    app.extensions['inventory_spec_previous_view'] = app.view_functions['expiry.dashboard']
    app.view_functions['expiry.dashboard'] = inventory_spec_dashboard
