import re
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import abort, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select

from product_display_master import lookup as product_lookup

ZERO = Decimal('0')
FACTORIES = {'12': '1·2공장', '3': '3공장', 'unknown': '공장 확인 필요'}
OVERRIDES = {'MedParkAllo': '3', 'MedParkAlloD': '3', 'S1-Allo 메디컬': '3', 'S1-Allo 덴탈': '3'}


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


def product_family(name):
    """Shared management family so related SKUs stay together across dashboards."""
    normalized = clean(name).upper().replace(' ', '')
    if 'ALLO' in normalized:
        return 'ALLO 계열'
    # BOSS/S1 are the CE/export display names of the same XB production family.
    if 'XB' in normalized or normalized in {'BOSS', 'S1'}:
        return 'XB 계열'
    rules = [
        ('XP', 'XP 계열'),
        ('OSS', 'OSS 계열'), ('DBM', 'DBM 계열'), ('SDERM', 'S DERM 계열'),
        ('SGEN', 'S GEN 계열'), ('FILL', 'FILL 계열'), ('ADITE', 'ADITE 계열'),
        ('TENDON', 'TENDON 계열'),
    ]
    for token, label in rules:
        if token in normalized:
            return label
    return clean(name) or '기타 제품'


def management_status(available, monthly_shipment, coverage_months, location_exception=ZERO):
    if location_exception > 0:
        return 'location', '위치 확인'
    if monthly_shipment > 0 and available <= 0:
        return 'stockout', '가용재고 없음'
    if coverage_months is not None and coverage_months < 1:
        return 'critical', '긴급 생산검토'
    if coverage_months is not None and coverage_months < 3:
        return 'watch', '생산계획 확인'
    if coverage_months is not None and coverage_months > 12:
        return 'excess', '과다재고 확인'
    if monthly_shipment <= 0 and available > 0:
        return 'no_demand', '출고이력 없음'
    return 'normal', '적정'


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
    custom = refs.get('product_factory', {}).get(name)
    if isinstance(custom, dict) and custom.get('factory') in {'12', '3'}:
        return custom['factory']
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
        state = 'unclassified'
    exception = factory == '3' and physical in {'finished12', 'work12'}
    if state == 'unavailable':
        bucket = 'unusable'
    elif exception:
        bucket = 'other'
    elif physical.startswith(('finished', 'work')):
        # Business rule: available stock is finished-goods warehouse + work-in-process warehouse only.
        bucket = 'available'
    else:
        bucket = 'other'
    return bucket, physical, exception


def cost_index(refs, target_month):
    from unit_cost_admin import cost_index_with_seed
    return cost_index_with_seed(refs, target_month)


def category_key(value):
    return ({'국내': 0, '일반수출': 1}.get(value, 2), natural(value))


def build_board(entries, refs, filters, snapshot_date, shipment_averages=None):
    shipment_averages = shipment_averages or {}
    selected = dict(q=clean(filters.get('q')), warehouses=list(filters.get('warehouses') or []),
                    product_factory=clean(filters.get('product_factory')),
                    availability=clean(filters.get('availability')),
                    show_value=bool(filters.get('show_value')),
                    shipment_period=int(filters.get('shipment_period') or 6))
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
        shown = product_lookup(code, raw.get('name'), raw.get('spec'), refs=refs)
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
        source_rows.append(dict(name=name, family=product_family(name), type=type_name, size=size, category=category,
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
            haystack = ' '.join(clean(row[key]) for key in ('name','family','type','size','category','erp','icube','original_spec','warehouse','place')).casefold()
            if selected['q'].casefold() not in haystack:
                continue
        rows.append(row)
    groups = {}
    missing_cost = set()
    unmapped = set()
    for row in rows:
        group = groups.setdefault((row['factory'], row['name']), dict(name=row['name'], factory=row['factory'],
            family=row['family'], tendon=row['name'].casefold() == 'tendon', lines={}, monthly_shipment=ZERO,
            shipment_erps=set(), **totals()))
        add_total(group, row)
        if row['erp'] and row['erp'] not in group['shipment_erps']:
            group['monthly_shipment'] += shipment_averages.get(row['erp'], ZERO)
            group['shipment_erps'].add(row['erp'])
        line_key = (row['category'], row['type'], row['size'])
        if not row['mapped']:
            line_key += (row['icube'] or row['erp'],)
            unmapped.add(row['icube'] or row['erp'])
        line = group['lines'].setdefault(line_key, dict(category=row['category'], type=row['type'], size=row['size'],
            original_specs=set(), places={}, monthly_shipment=ZERO, shipment_erps=set(), **totals()))
        add_total(line, row)
        if row['erp'] and row['erp'] not in line['shipment_erps']:
            line['monthly_shipment'] += shipment_averages.get(row['erp'], ZERO)
            line['shipment_erps'].add(row['erp'])
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
            group.pop('shipment_erps', None)
            group['coverage_months'] = group['available'] / group['monthly_shipment'] if group['monthly_shipment'] > 0 else None
            group['management_key'], group['management_label'] = management_status(
                group['available'], group['monthly_shipment'], group['coverage_months'], group['location_exception'])
            lines = list(group.pop('lines').values())
            for line in lines:
                line.pop('shipment_erps', None)
                line['coverage_months'] = line['available'] / line['monthly_shipment'] if line['monthly_shipment'] > 0 else None
                line['management_key'], line['management_label'] = management_status(
                    line['available'], line['monthly_shipment'], line['coverage_months'], line['location_exception'])
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
                    categories.append(dict(name=row['category'], rows=[], monthly_shipment=ZERO, **totals()))
                category = categories[-1]
                category['rows'].append(row)
                for field in totals():
                    category[field] += row[field]
                category['monthly_shipment'] += row['monthly_shipment']
            for category in categories:
                category['coverage_months'] = (category['available'] / category['monthly_shipment']
                                               if category['monthly_shipment'] > 0 else None)
                previous_type = None
                for row in category['rows']:
                    row['type_start'] = row['type'] != previous_type
                    previous_type = row['type']
            group['categories'] = categories
            for field in totals():
                section_total[field] += group[field]
                overall[field] += group[field]
        if products:
            section_total['monthly_shipment'] = sum((g['monthly_shipment'] for g in products), ZERO)
            section_total['coverage_months'] = (section_total['available'] / section_total['monthly_shipment']
                                                if section_total['monthly_shipment'] > 0 else None)
            families = []
            family_index = {}
            for group in products:
                family = family_index.get(group['family'])
                if family is None:
                    family = dict(label=group['family'], products=[], monthly_shipment=ZERO,
                                  status_counts={}, **totals())
                    family_index[group['family']] = family
                    families.append(family)
                family['products'].append(group)
                family['monthly_shipment'] += group['monthly_shipment']
                family['status_counts'][group['management_key']] = family['status_counts'].get(group['management_key'], 0) + 1
                for field in totals():
                    family[field] += group[field]
            family_rank = {'XB 계열': 0, 'XP 계열': 1, 'ALLO 계열': 2, 'OSS 계열': 3,
                           'DBM 계열': 4, 'S DERM 계열': 5, 'S GEN 계열': 6}
            families.sort(key=lambda family: (family_rank.get(family['label'], 99), natural(family['label'])))
            for family in families:
                family['coverage_months'] = (family['available'] / family['monthly_shipment']
                                             if family['monthly_shipment'] > 0 else None)
            sections.append(dict(key=factory, label=factory_label, products=products, families=families, **section_total))
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
    overall['monthly_shipment'] = sum((group['monthly_shipment'] for section in sections for group in section['products'] if group['monthly_shipment'] > 0), ZERO)
    overall['coverage_months'] = overall['available'] / overall['monthly_shipment'] if overall['monthly_shipment'] > 0 else None
    management = {key: [] for key in ('stockout','critical','watch','excess','no_demand','location')}
    for section in sections:
        for group in section['products']:
            if group['management_key'] in management:
                management[group['management_key']].append(group)
    return dict(filters=selected, sections=sections, metrics=overall, warehouses=warehouses,
                invalid_rows=invalid_rows, excluded_rows=excluded_rows, missing_cost=len(missing_cost),
                unmapped=len(unmapped), source_rows=len(rows), snapshot_date=snapshot_date,
                product_count=sum(len(section['products']) for section in sections), management=management)


def install_inventory_spec_view(app, db):
    models = app.extensions['expiry_models']
    Reference = models['Reference']
    Import = models['Import']
    app.jinja_env.filters['iv_qty'] = qty_filter
    app.jinja_env.filters['iv_won'] = won_filter
    from product_order_admin import install_product_order_admin
    install_product_order_admin(app, db)
    from order_fulfillment import install_order_fulfillment
    install_order_fulfillment(app, db)

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
            try:
                shipment_period = int(request.args.get('shipment_period', 6))
            except ValueError:
                shipment_period = 6
            if shipment_period not in {3, 6, 12}:
                shipment_period = 6
            from shipment_analysis import shipment_average_index
            shipment_averages, shipment_meta = shipment_average_index(app, db, shipment_period)
            filters = dict(q=request.args.get('q', ''), warehouses=request.args.getlist('warehouse'),
                product_factory=request.args.get('product_factory', ''), availability=request.args.get('availability', ''),
                show_value=True, shipment_period=shipment_period)
            board = build_board(snapshot.payload or [], refs, filters, snapshot.as_of, shipment_averages)
            board['shipment'] = shipment_meta
        return render_template('inventory_specs.html', board=board, factories=FACTORIES,
                               can_edit=current_user.role in {'admin','editor'},
                               snapshot_id=snapshot.id if snapshot is not None else '')

    if 'expiry.dashboard' not in app.view_functions:
        raise RuntimeError('Existing inventory dashboard endpoint is missing')
    app.extensions['inventory_spec_previous_view'] = app.view_functions['expiry.dashboard']
    app.view_functions['expiry.dashboard'] = inventory_spec_dashboard
