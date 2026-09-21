"""Order/quotation fulfillment dashboard joined to the latest inventory snapshot."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
import base64
import gzip
import re
import uuid

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from inventory_spec_view import clean, factory_for, natural
from product_display_master import lookup

ZERO = Decimal('0')
DATA_DIR = Path(__file__).with_name('data')

QUOTE_FIELDS = {
    '견적일자':'date','견적번호':'document','고객코드':'customer_code','고객명':'customer',
    '거래구분명':'trade','환종':'currency','견적요청자':'requester','담당자':'owner','순번':'line',
    '품번':'erp','품명':'item_name','규격':'spec','납기일자':'due','관리단위':'unit','견적수량':'quantity',
    '관리구분':'management','비고(건)':'note','비고 (내역)':'detail','지역명':'region','고객분류명':'customer_class'
}
ORDER_FIELDS = {
    '주문일자':'date','주문번호':'document','고객':'customer','거래구분':'trade','환종':'currency',
    '납품처':'destination','담당자':'owner','순번':'line','품번':'erp','품명':'item_name','규격':'spec',
    '납기일자':'due','출하예정일자':'ship_due','관리단위':'unit','주문수량':'quantity',
    '관리구분':'management','관리번호':'management_no'
}
MOVEMENT_FIELDS = {
    'No':'no','이동번호':'document','이동일자':'date','품번':'erp','품명':'item_name','규격':'spec',
    '재고단위':'unit','이동수량':'quantity','출고창고':'from_warehouse','출고장소':'from_location',
    '입고창고':'to_warehouse','입고장소':'to_location','이동담당자':'owner','LOT No.':'lot'
}


def number(value):
    try:
        out = Decimal(clean(value).replace(',', '') or '0')
    except InvalidOperation as exc:
        raise ValueError('수량이 숫자가 아닙니다.') from exc
    if not out.is_finite():
        raise ValueError('수량이 유효하지 않습니다.')
    return out


def markdown_rows(content, kind):
    fields = QUOTE_FIELDS if kind == 'quote_register' else ORDER_FIELDS
    lines = [line for line in content.lstrip('\ufeff').splitlines() if line.strip().startswith('|')]
    if len(lines) < 3:
        raise ValueError('열 제목을 포함한 마크다운 표를 붙여넣어 주세요.')
    cells = lambda line: [clean(value).replace('\\~', '~').replace('　', '') for value in line.strip().strip('|').split('|')]
    header = cells(lines[0])
    missing = [name for name in fields if name not in header]
    if missing:
        raise ValueError('필수 열이 없습니다: ' + ', '.join(missing))
    index = {name: header.index(name) for name in fields}
    result, errors = [], []
    for row_no, line in enumerate(lines[2:], 3):
        values = cells(line)
        if len(values) != len(header):
            errors.append(f'{row_no}행 열 개수 불일치')
            continue
        row = {target: values[index[source]] for source, target in fields.items()}
        try:
            date.fromisoformat(row['date'])
            if row.get('due'):
                date.fromisoformat(row['due'])
            row['quantity'] = str(number(row['quantity']))
        except ValueError as error:
            errors.append(f'{row_no}행: {error}')
            continue
        if not row['document'] or not row['line'] or not row['erp']:
            errors.append(f'{row_no}행: 문서번호·순번·품번이 필요합니다.')
            continue
        result.append(row)
    if errors:
        suffix = f' 외 {len(errors)-10}건' if len(errors) > 10 else ''
        raise ValueError(' / '.join(errors[:10]) + suffix)
    if not result:
        raise ValueError('적용할 자료가 없습니다.')
    return result


def movement_rows(content):
    import csv
    import io
    raw = list(csv.reader(io.StringIO(content.lstrip('\ufeff')), delimiter='\t'))
    if not raw:
        raise ValueError('파일이 비어 있습니다.')
    header = [clean(value) for value in raw[0]]
    missing = [name for name in MOVEMENT_FIELDS if name not in header]
    if missing:
        raise ValueError('재고이동 필수 열이 없습니다: ' + ', '.join(missing))
    index = {name:header.index(name) for name in MOVEMENT_FIELDS}
    result, errors = [], []
    for row_no, values in enumerate(raw[1:], 2):
        if not any(clean(value) for value in values):
            continue
        if len(values) != len(header):
            errors.append(f'{row_no}행 열 개수 불일치')
            continue
        row = {target:clean(values[index[source]]) for source,target in MOVEMENT_FIELDS.items()}
        try:
            date.fromisoformat(row['date'])
            row['quantity'] = str(number(row['quantity']))
        except ValueError as error:
            errors.append(f'{row_no}행: {error}')
            continue
        row['line'] = row['no']
        if not row['document'] or not row['line'] or not row['erp']:
            errors.append(f'{row_no}행: 이동번호·No·품번이 필요합니다.')
            continue
        result.append(row)
    if errors:
        more = f' 외 {len(errors)-10}건' if len(errors) > 10 else ''
        raise ValueError(' / '.join(errors[:10]) + more)
    if not result:
        raise ValueError('적용할 재고이동 자료가 없습니다.')
    return result


@lru_cache(maxsize=3)
def seed(kind):
    filenames = {'quote_register':'quotes_2026.md.gz.b64','sales_order':'orders_2026.md.gz.b64',
                 'shipment_detail':'shipment_detail_2026.tsv.gz.b64'}
    filename = filenames.get(kind)
    if not filename:
        return []
    path = DATA_DIR / filename
    if not path.exists():
        return []
    content = gzip.decompress(base64.b64decode(path.read_text(encoding='ascii'))).decode('utf-8-sig')
    if kind == 'shipment_detail':
        from shipment_analysis import parse_shipment_detail
        return parse_shipment_detail(content, filename)[0] or []
    return markdown_rows(content, kind)


def merge_rows(before, incoming):
    merged = {(row['document'], row['line']): row for row in before}
    for row in incoming:
        merged[(row['document'], row['line'])] = row
    return sorted(merged.values(), key=lambda row: (row['date'], row['document'], natural(row['line'])))


def stock_index(stock, refs):
    result = defaultdict(lambda: {'finished':ZERO, 'waiting':ZERO, 'consignment':ZERO, 'factory':'unknown'})
    mapping = refs.get('mapping', {})
    for entry in stock.payload if stock is not None else []:
        raw = entry.get('data', {}) if isinstance(entry, dict) else {}
        if not isinstance(raw, dict) or clean(raw.get('account')) != '제품' or clean(raw.get('unit')).upper() != 'EA':
            continue
        try:
            qty = number(raw.get('quantity'))
        except ValueError:
            continue
        if qty <= 0:
            continue
        erp = clean(raw.get('erp'))
        if not erp:
            continue
        warehouse = re.sub(r'\s+', '', clean(raw.get('warehouse'))).casefold()
        location = re.sub(r'\s+', '', clean(raw.get('location'))).casefold()
        if '완제품창고' not in warehouse and '수탁창고' not in warehouse:
            continue
        mapped = mapping.get(erp) or {}
        code = clean(mapped.get('icube') or raw.get('icube')).upper()
        shown = lookup(code, raw.get('name'), raw.get('spec'), refs=refs)
        fac = factory_for(code, clean(shown.get('name') or raw.get('name')), raw, refs)
        result[erp]['factory'] = fac
        if '수탁' in warehouse or '수탁' in location:
            result[erp]['consignment'] += qty
        elif '출하대기' in location:
            result[erp]['waiting'] += qty
        else:
            result[erp]['finished'] += qty
    return result


def fulfillment_key(customer, erp):
    return (re.sub(r'\s+', '', clean(customer)).casefold(), clean(erp).upper())


def apply_fifo_fulfillment(rows, shipment_rows, scope, as_of=None):
    """Apply actual shipments to customer+ERP demand in chronological FIFO order."""
    if scope == 'overseas':
        # Overseas physical shipments are also registered as "예외출고" in iCUBE.
        # The T/T trade type is the reliable overseas boundary; shipment_type is not.
        valid = lambda row: clean(row.get('trade')).upper() == 'T/T'
    elif scope == 'domestic':
        valid = lambda row: clean(row.get('trade')).upper() == 'DOMESTIC' and clean(row.get('shipment_type')) == '국내수주'
    else:
        return
    events = defaultdict(list)
    for row in rows:
        row['original_quantity'] = row['quantity']
        row['fulfilled'] = ZERO
        row['remaining'] = row['quantity']
        events[fulfillment_key(row['customer'], row['erp'])].append(
            (row['date'], 0, row['document'], natural(row['line']), 'demand', row))
    for shipment in shipment_rows or []:
        if not valid(shipment) or clean(shipment.get('unit')).upper() != 'EA':
            continue
        if as_of and clean(shipment.get('date')) > as_of:
            continue
        try:
            qty = number(shipment.get('quantity'))
        except ValueError:
            continue
        events[fulfillment_key(shipment.get('customer'), shipment.get('erp'))].append(
            (shipment['date'], 1, shipment.get('document',''), natural(shipment.get('line','')), 'shipment', qty))
    for key_events in events.values():
        queue, fulfilled_stack = [], []
        for _, _, _, _, kind, value in sorted(key_events, key=lambda event: event[:4]):
            if kind == 'demand':
                queue.append(value)
                continue
            qty = value
            if qty > 0:
                while qty > 0 and queue:
                    target = queue[0]
                    used = min(qty, target['remaining'])
                    target['fulfilled'] += used
                    target['remaining'] -= used
                    qty -= used
                    fulfilled_stack.append((target, used))
                    if target['remaining'] <= 0:
                        queue.pop(0)
            elif qty < 0:
                returned = -qty
                while returned > 0 and fulfilled_stack:
                    target, used = fulfilled_stack.pop()
                    restored = min(returned, used)
                    target['fulfilled'] -= restored
                    target['remaining'] += restored
                    returned -= restored
                    if target not in queue:
                        queue.insert(0, target)
                    if used > restored:
                        fulfilled_stack.append((target, used - restored))


def apply_consignment_movements(rows, movements, as_of=None):
    """Apply net transfers into each customer-named consignment location."""
    events = defaultdict(list)
    for row in rows:
        row['original_quantity'] = row['quantity']
        row['fulfilled'] = ZERO
        row['remaining'] = row['quantity']
        events[fulfillment_key(row['customer'], row['erp'])].append(
            (row['date'], 0, row['document'], natural(row['line']), 'demand', row))
    for movement in movements or []:
        if clean(movement.get('unit')).upper() != 'EA':
            continue
        if as_of and clean(movement.get('date')) > as_of:
            continue
        from_wh = re.sub(r'\s+', '', clean(movement.get('from_warehouse')))
        to_wh = re.sub(r'\s+', '', clean(movement.get('to_warehouse')))
        customer, multiplier = '', ZERO
        if '수탁' in to_wh:
            customer, multiplier = movement.get('to_location'), Decimal('1')
        elif '수탁' in from_wh:
            customer, multiplier = movement.get('from_location'), Decimal('-1')
        if not customer:
            continue
        qty = number(movement.get('quantity')) * multiplier
        events[fulfillment_key(customer, movement.get('erp'))].append(
            (movement['date'], 1, movement.get('document',''), natural(movement.get('line','')), 'movement', qty))
    for key_events in events.values():
        queue, fulfilled_stack = [], []
        for _, _, _, _, kind, value in sorted(key_events, key=lambda event:event[:4]):
            if kind == 'demand':
                queue.append(value)
                continue
            qty = value
            if qty > 0:
                while qty > 0 and queue:
                    target = queue[0]
                    used = min(qty, target['remaining'])
                    target['fulfilled'] += used
                    target['remaining'] -= used
                    qty -= used
                    fulfilled_stack.append((target, used))
                    if target['remaining'] <= 0:
                        queue.pop(0)
            elif qty < 0:
                returned = -qty
                while returned > 0 and fulfilled_stack:
                    target, used = fulfilled_stack.pop()
                    restored = min(returned, used)
                    target['fulfilled'] -= restored
                    target['remaining'] += restored
                    returned -= restored
                    if target not in queue:
                        queue.insert(0, target)
                    if used > restored:
                        fulfilled_stack.append((target, used-restored))


def allocate_waiting_stock(rows, stock_by_erp):
    """Allocate current shipment-waiting stock to oldest open export quotes by ERP."""
    by_erp = defaultdict(list)
    for row in rows:
        row['prepared'] = ZERO
        if row.get('remaining', ZERO) > 0:
            by_erp[row['erp']].append(row)
    for erp, demands in by_erp.items():
        waiting = stock_by_erp[erp]['waiting']
        for row in sorted(demands, key=lambda item:(item['date'], item['document'], natural(item['line']))):
            if waiting <= 0:
                break
            allocated = min(waiting, row['remaining'])
            row['prepared'] = allocated
            waiting -= allocated


def build_board(orders, quotes, shipments, movements, stock, refs, saved, filters):
    """Build one board from exactly one business document stream."""
    scope = clean(filters.get('scope')) or 'overseas'
    if scope not in {'overseas', 'consignment', 'domestic'}:
        scope = 'overseas'
    query = clean(filters.get('q')).casefold()
    start, end = filters.get('start'), filters.get('end')
    stock_by_erp = stock_index(stock, refs)
    mapping = refs.get('mapping', {})
    if scope == 'overseas':
        sources = [row for row in quotes if clean(row.get('trade')) == '수출']
        source_name, key_prefix = '해외 견적등록', 'export'
    elif scope == 'consignment':
        sources = [row for row in quotes if clean(row.get('trade')) == '국내'
                   and '수탁' in (clean(row.get('note')) + ' ' + clean(row.get('detail')))]
        source_name, key_prefix = '국내 수탁 견적등록', 'consignment'
    else:
        sources = [row for row in orders if clean(row.get('trade')).upper() == 'DOMESTIC']
        source_name, key_prefix = '국내 주문등록', 'domestic'

    lines = []
    for source in sources:
        row = dict(source)
        row['quantity'] = number(row['quantity'])
        item_stock = stock_by_erp[row['erp']]
        row.update(item_stock)
        mapped = mapping.get(row['erp']) or {}
        shown = lookup(clean(mapped.get('icube')).upper(), row['item_name'], row['spec'], refs=refs)
        row['display_name'] = clean(shown.get('name') or row['item_name'])
        row['display_size'] = clean(shown.get('size') or row['spec'])
        row['key'] = key_prefix + '|' + row['document'] + '|' + row['line']
        progress = saved.get(row['key']) if isinstance(saved.get(row['key']), dict) else {}
        row['prepared'] = ZERO
        row['expected_date'] = clean(progress.get('expected_date'))
        row['note'] = clean(progress.get('note'))
        row['status'] = clean(progress.get('status'))
        if scope == 'consignment':
            row['status'], row['shortage'] = '미이동 잔량', None
        lines.append(row)

    cutoff = end or date.today().isoformat()
    if scope == 'consignment':
        apply_consignment_movements(lines, movements, cutoff)
    elif shipments:
        apply_fifo_fulfillment(lines, shipments, scope, cutoff)
    elif scope in {'overseas','domestic'}:
        try:
            from shipment_fulfillment_seed import FULFILLED
        except ImportError:
            FULFILLED = {}
        for row in lines:
            row['original_quantity'] = row['quantity']
            completed = min(number(FULFILLED.get(row['key'])), row['quantity'])
            row['fulfilled'] = completed
            row['remaining'] = row['quantity'] - completed
    period_lines = []
    for row in lines:
        if (start and row['date'] < start) or (end and row['date'] > end):
            continue
        if row['remaining'] <= 0:
            continue
        period_lines.append(row)
    if scope == 'overseas':
        allocate_waiting_stock(period_lines, stock_by_erp)
    visible = []
    for row in period_lines:
        if query and query not in ' '.join(clean(row.get(k)) for k in ('document','customer','owner','erp','item_name','spec')).casefold():
            continue
        if scope == 'overseas':
            row['shortage'] = max(row['remaining'] - row['prepared'], ZERO)
            row['status'] = ('준비완료' if row['shortage'] <= 0 else
                             '일부 준비' if row['prepared'] > 0 else '준비중')
        elif scope == 'domestic':
            row['status'], row['shortage'] = '미출고 잔량', None
        visible.append(row)
    lines = visible

    lines.sort(key=lambda row: (row['customer'], row.get('ship_due') or row.get('due') or '9999', row['document'], natural(row['line'])))
    grouped = {}
    for row in lines:
        due = row.get('ship_due') or row.get('due') or ''
        group_key = (row['customer'], due)
        if group_key not in grouped:
            grouped[group_key] = {'customer':row['customer'],'due':due,'rows':[], 'documents':set(),
                                  'owners':set(),'quantity':ZERO,'prepared':ZERO,'shortage':ZERO}
        group = grouped[group_key]
        group['rows'].append(row)
        group['documents'].add(row['document'])
        if row.get('owner'):
            group['owners'].add(row['owner'])
        group['quantity'] += row['remaining']
        group['prepared'] += row['prepared']
        if row['shortage'] is not None:
            group['shortage'] += row['shortage']
    groups = sorted(grouped.values(), key=lambda group: (group['due'] or '9999', group['customer']))
    for group in groups:
        group['documents'] = sorted(group['documents'])
        group['owners'] = sorted(group['owners'])
    return {'scope':scope,'source_name':source_name,'groups':groups,'lines':lines,
            'customers':len({g['customer'] for g in groups}),'group_count':len(groups),
            'documents':len({row['document'] for row in lines}),
            'quantity':sum((g['quantity'] for g in groups),ZERO),
            'prepared':sum((g['prepared'] for g in groups),ZERO),
            'shortage':sum((g['shortage'] for g in groups),ZERO)}


def install_order_fulfillment(app, db):
    if app.config.get('ORDER_FULFILLMENT_INSTALLED'):
        return
    app.config['ORDER_FULFILLMENT_INSTALLED'] = True
    Reference, Import = app.extensions['expiry_models']['Reference'], app.extensions['expiry_models']['Import']

    def latest(kind):
        return db.session.scalar(select(Import).where(Import.kind == kind, Import.committed_at.is_not(None))
            .order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))

    def source(kind):
        saved = latest(kind)
        return (saved.payload, saved) if saved is not None else (seed(kind), None)

    def refs():
        result = {}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    @login_required
    def view():
        if request.method == 'POST':
            if current_user.role not in {'admin','editor'}:
                abort(403)
            action = request.form.get('action')
            if action == 'import':
                kind = request.form.get('kind')
                if kind not in {'quote_register','sales_order','inventory_movement'}:
                    abort(400)
                content = request.form.get('pasted','')
                upload = request.files.get('data_file')
                if upload is not None and upload.filename:
                    raw = upload.stream.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        flash('파일은 8MB 이하만 등록할 수 있습니다.', 'error')
                        return redirect(url_for('expiry.order_fulfillment'))
                    content = raw.decode('utf-8-sig')
                try:
                    incoming = movement_rows(content) if kind == 'inventory_movement' else markdown_rows(content, kind)
                    previous, _ = source(kind)
                    rows = incoming if request.form.get('mode') == 'replace' else merge_rows(previous, incoming)
                except (ValueError, UnicodeDecodeError) as error:
                    flash(str(error), 'error')
                    return redirect(url_for('expiry.order_fulfillment'))
                as_of = max(date.fromisoformat(row['date']) for row in rows)
                db.session.execute(text('SELECT pg_advisory_xact_lock(73190426)'))
                record = Import(id=str(uuid.uuid4()),kind=kind,payload=rows,
                    notes={'rows':len(rows),'mode':request.form.get('mode','merge')},base_revision='order-fulfillment-v1',
                    as_of=as_of,created_by=current_user.id,committed_at=datetime.now(timezone.utc))
                db.session.add(record); db.session.commit()
                title = {'quote_register':'견적등록','sales_order':'주문현황','inventory_movement':'재고이동현황'}[kind]
                flash(f'{title} {len(rows):,}행을 적용했습니다.', 'success')
                return redirect(url_for('expiry.order_fulfillment'))
            if action == 'progress':
                key = clean(request.form.get('key'))
                if not key.startswith('export|'):
                    abort(400)
                db.session.execute(text('SELECT pg_advisory_xact_lock(73190426)'))
                row = db.session.scalar(select(Reference).where(Reference.kind=='order_progress',Reference.key==key))
                if row is None:
                    row=Reference(kind='order_progress',key=key);db.session.add(row)
                row.payload={'expected_date':clean(request.form.get('expected_date')),
                             'note':clean(request.form.get('note'))}
                row.updated_by=current_user.id;row.updated_at=datetime.now(timezone.utc)
                db.session.commit();flash('준비현황을 저장했습니다.','success')
                return redirect(url_for('expiry.order_fulfillment'))
            abort(400)
        orders, order_import = source('sales_order')
        quotes, quote_import = source('quote_register')
        shipments, shipment_import = source('shipment_detail')
        movements, movement_import = source('inventory_movement')
        scope = clean(request.args.get('scope')) or 'overseas'
        relevant = quotes if scope in {'overseas','consignment'} else orders
        latest_date = max(date.fromisoformat(row['date']) for row in relevant)
        start = clean(request.args.get('start')) or latest_date.replace(day=1).isoformat()
        end = clean(request.args.get('end')) or latest_date.isoformat()
        all_refs = refs()
        progress = all_refs.get('order_progress', {})
        board = build_board(orders, quotes, shipments, movements, latest('stock'), all_refs, progress,
                            {'q':request.args.get('q'),'scope':scope,'start':start,'end':end})
        return render_template('order_fulfillment.html',board=board,start=start,end=end,
            order_import=order_import,quote_import=quote_import,shipment_import=shipment_import,movement_import=movement_import,
            can_edit=current_user.role in {'admin','editor'})

    app.add_url_rule('/expiry/order-fulfillment',endpoint='expiry.order_fulfillment',view_func=view,methods=['GET','POST'])
