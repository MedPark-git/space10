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


@lru_cache(maxsize=2)
def seed(kind):
    filename = 'quotes_2026.md.gz.b64' if kind == 'quote_register' else 'orders_2026.md.gz.b64'
    path = DATA_DIR / filename
    if not path.exists():
        return []
    content = gzip.decompress(base64.b64decode(path.read_text(encoding='ascii'))).decode('utf-8-sig')
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


def build_board(orders, quotes, stock, refs, saved, filters):
    query = clean(filters.get('q')).casefold()
    trade = clean(filters.get('trade')).upper()
    start = filters.get('start')
    end = filters.get('end')
    stock_by_erp = stock_index(stock, refs)
    mapping = refs.get('mapping', {})
    lines = []
    for source in orders:
        if start and source['date'] < start or end and source['date'] > end:
            continue
        if trade and source['trade'].upper() != trade:
            continue
        if query and query not in ' '.join(clean(source.get(k)) for k in ('document','customer','owner','erp','item_name','spec')).casefold():
            continue
        row = dict(source)
        row['quantity'] = number(row['quantity'])
        item_stock = stock_by_erp[row['erp']]
        row.update(item_stock)
        mapped = mapping.get(row['erp']) or {}
        shown = lookup(clean(mapped.get('icube')).upper(), row['item_name'], row['spec'], refs=refs)
        row['display_name'] = clean(shown.get('name') or row['item_name'])
        row['display_size'] = clean(shown.get('size') or row['spec'])
        key = 'order|' + row['document'] + '|' + row['line']
        progress = saved.get(key) if isinstance(saved.get(key), dict) else {}
        row['prepared'] = number(progress.get('prepared'))
        row['expected_date'] = clean(progress.get('expected_date'))
        row['note'] = clean(progress.get('note'))
        row['status'] = clean(progress.get('status')) or ('재고 가능' if item_stock['finished'] + item_stock['waiting'] >= row['quantity'] else '재고 부족')
        row['shortage'] = max(row['quantity'] - row['prepared'], ZERO)
        row['key'] = key
        lines.append(row)
    lines.sort(key=lambda row: (row.get('ship_due') or row.get('due') or '9999', row['document'], natural(row['line'])))
    groups = []
    for row in lines:
        if not groups or groups[-1]['document'] != row['document']:
            groups.append({'document':row['document'],'date':row['date'],'customer':row['customer'],'trade':row['trade'],
                           'owner':row['owner'],'due':row.get('ship_due') or row.get('due'),'rows':[],'quantity':ZERO,'prepared':ZERO,'shortage':ZERO})
        group = groups[-1]
        group['rows'].append(row)
        for field in ('quantity','prepared','shortage'):
            group[field] += row[field]

    consignments = []
    for source in quotes:
        note = clean(source.get('note')) + ' ' + clean(source.get('detail'))
        if source.get('trade') != '국내' or '수탁' not in note:
            continue
        if start and source['date'] < start or end and source['date'] > end:
            continue
        row = dict(source)
        row['quantity'] = number(row['quantity'])
        item_stock = stock_by_erp[row['erp']]
        row.update(item_stock)
        row['shortage'] = max(row['quantity'] - item_stock['consignment'], ZERO)
        consignments.append(row)
    consignments.sort(key=lambda row: (row['due'] or '9999', row['document'], natural(row['line'])))
    return {'groups':groups,'lines':lines,'consignments':consignments,
            'orders':len(groups),'quantity':sum((g['quantity'] for g in groups),ZERO),
            'prepared':sum((g['prepared'] for g in groups),ZERO),
            'shortage':sum((g['shortage'] for g in groups),ZERO),
            'tt_orders':sum(g['trade'].upper()=='T/T' for g in groups),
            'domestic_orders':sum(g['trade'].upper()=='DOMESTIC' for g in groups)}


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
                if kind not in {'quote_register','sales_order'}:
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
                    incoming = markdown_rows(content, kind)
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
                flash(f'{"견적등록" if kind=="quote_register" else "주문현황"} {len(rows):,}행을 적용했습니다.', 'success')
                return redirect(url_for('expiry.order_fulfillment'))
            if action == 'progress':
                key = clean(request.form.get('key'))
                if not key.startswith('order|'):
                    abort(400)
                prepared = number(request.form.get('prepared'))
                status = clean(request.form.get('status'))
                if prepared < 0 or status not in {'신규','재고 확인','재고 부족','생산 대기','일부 준비','준비완료','출하 승인','출하완료'}:
                    abort(400)
                db.session.execute(text('SELECT pg_advisory_xact_lock(73190426)'))
                row = db.session.scalar(select(Reference).where(Reference.kind=='order_progress',Reference.key==key))
                if row is None:
                    row=Reference(kind='order_progress',key=key);db.session.add(row)
                row.payload={'prepared':str(prepared),'expected_date':clean(request.form.get('expected_date')),
                             'status':status,'note':clean(request.form.get('note'))}
                row.updated_by=current_user.id;row.updated_at=datetime.now(timezone.utc)
                db.session.commit();flash('준비현황을 저장했습니다.','success')
                return redirect(url_for('expiry.order_fulfillment'))
            abort(400)
        orders, order_import = source('sales_order')
        quotes, quote_import = source('quote_register')
        latest_date = max(date.fromisoformat(row['date']) for row in orders)
        start = clean(request.args.get('start')) or latest_date.replace(day=1).isoformat()
        end = clean(request.args.get('end')) or latest_date.isoformat()
        all_refs = refs()
        progress = all_refs.get('order_progress', {})
        board = build_board(orders, quotes, latest('stock'), all_refs, progress,
                            {'q':request.args.get('q'),'trade':request.args.get('trade'),'start':start,'end':end})
        return render_template('order_fulfillment.html',board=board,start=start,end=end,
            order_import=order_import,quote_import=quote_import,can_edit=current_user.role in {'admin','editor'})

    app.add_url_rule('/expiry/order-fulfillment',endpoint='expiry.order_fulfillment',view_func=view,methods=['GET','POST'])
