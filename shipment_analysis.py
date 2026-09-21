"""Shipment averages and inventory coverage, isolated from existing stock writes."""
import csv
import io
import re
import uuid
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from inventory_spec_view import clean, display, factory_for, natural, stock_bucket
from product_display_master import lookup
from shipment_seed import ROWS as SEED_ROWS

ZERO = Decimal('0')
MAX_FILE_SIZE = 4 * 1024 * 1024


def number(value):
    try:
        result = Decimal(str(value or '0').replace(',', '').strip())
    except InvalidOperation as exc:
        raise ValueError('수량이 숫자가 아닙니다.') from exc
    if not result.is_finite():
        raise ValueError('수량이 유효한 숫자가 아닙니다.')
    return result


def _markdown_cells(line):
    body = line.strip().strip('|')
    return [cell.strip() for cell in body.split('|')]


def parse_shipments(content, filename=''):
    content = content.lstrip('\ufeff')
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        raise ValueError('파일이 비어 있습니다.')
    markdown = lines[0].lstrip().startswith('|')
    if markdown:
        raw = [_markdown_cells(line) for line in lines]
    else:
        delimiter = '\t' if '\t' in lines[0] or filename.lower().endswith('.tsv') else ','
        raw = list(csv.reader(io.StringIO(content), delimiter=delimiter))
    header = [clean(value) for value in raw[0]]
    if header != ['출고일자', '품번', '출고수량']:
        raise ValueError('첫 행의 열 순서가 출고일자·품번·출고수량과 일치해야 합니다.')
    grouped, errors, negative = defaultdict(Decimal), [], 0
    for line_no, values in enumerate(raw[1:], 2):
        if len(values) == 3 and all(re.fullmatch(r':?-{2,}:?', clean(v)) for v in values):
            continue
        if len(values) != 3:
            errors.append(f'{line_no}행: 열이 {len(values)}개입니다.')
            continue
        try:
            shipped = date.fromisoformat(clean(values[0]))
            erp = clean(values[1])
            quantity = number(values[2])
            if not erp:
                raise ValueError('품번이 없습니다.')
        except (ValueError, TypeError) as error:
            errors.append(f'{line_no}행: {error}')
            continue
        grouped[(shipped, erp)] += quantity
        negative += quantity < 0
    if errors:
        more = f' 외 {len(errors)-10}건' if len(errors) > 10 else ''
        raise ValueError(' / '.join(errors[:10]) + more)
    if not grouped:
        raise ValueError('적용할 출고내역이 없습니다.')
    rows = [dict(date=shipped.isoformat(), erp=erp, quantity=str(quantity))
            for (shipped, erp), quantity in sorted(grouped.items())]
    return rows, negative


def seed_rows():
    return [dict(date=day, erp=erp, quantity=quantity) for day, erp, quantity in SEED_ROWS]


def shipment_markdown(rows):
    output = ['| 출고일자 | 품번 | 출고수량 |', '| --- | --- | --- |']
    for row in rows:
        output.append(f"| {row['date']} | {row['erp']} | {row['quantity']} |")
    return '\n'.join(output) + '\n'


def month_sequence(end_month, count):
    year, month = map(int, end_month.split('-'))
    result = []
    for _ in range(count):
        result.append(f'{year:04d}-{month:02d}')
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return list(reversed(result))


def complete_month_end(latest):
    last_day = monthrange(latest.year, latest.month)[1]
    if latest.day == last_day:
        return latest.strftime('%Y-%m')
    prior = latest.replace(day=1) - timedelta(days=1)
    return prior.strftime('%Y-%m')


def shipment_average_index(app, db, period=6):
    """Return one monthly average per ERP code for dashboard use."""
    if period not in {3, 6, 12}:
        period = 6
    Import = app.extensions['expiry_models']['Import']
    saved = db.session.scalar(select(Import).where(
        Import.kind == 'shipment', Import.committed_at.is_not(None)
    ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))
    rows = saved.payload if saved is not None else seed_rows()
    parsed = [(date.fromisoformat(row['date']), clean(row['erp']), number(row['quantity'])) for row in rows]
    if not parsed:
        return {}, dict(period=period, months=[], source=None)
    first_month = min(day for day, _, _ in parsed).strftime('%Y-%m')
    end_month = complete_month_end(max(day for day, _, _ in parsed))
    months = [month for month in month_sequence(end_month, period) if month >= first_month]
    monthly = defaultdict(Decimal)
    for day, erp, quantity in parsed:
        monthly[(erp, day.strftime('%Y-%m'))] += quantity
    codes = {erp for _, erp, _ in parsed}
    averages = {
        erp: sum((monthly.get((erp, month), ZERO) for month in months), ZERO) / len(months)
        for erp in codes
    } if months else {}
    return averages, dict(period=period, months=months, source=saved)


def build_analysis(shipment_rows, stock_payload, refs, selected_period=6):
    parsed = [(date.fromisoformat(row['date']), clean(row['erp']), number(row['quantity']))
              for row in shipment_rows]
    earliest = min(day for day, _, _ in parsed)
    latest = max(day for day, _, _ in parsed)
    completed_end = complete_month_end(latest)
    first_month = earliest.strftime('%Y-%m')
    windows = {}
    for period in (3, 6, 12):
        months = [month for month in month_sequence(completed_end, period) if month >= first_month]
        windows[period] = months
    monthly = defaultdict(Decimal)
    shipment_codes = set()
    negative_rows = 0
    for day, erp, quantity in parsed:
        monthly[(erp, day.strftime('%Y-%m'))] += quantity
        shipment_codes.add(erp)
        negative_rows += quantity < 0

    mapping = refs.get('mapping', {})
    inventory = defaultdict(lambda: dict(available=ZERO, total=ZERO, raw=None, icube=''))
    for entry in stock_payload or []:
        raw = entry.get('data', {}) if isinstance(entry, dict) else {}
        if not isinstance(raw, dict) or clean(raw.get('account')) != '제품':
            continue
        try:
            quantity = number(raw.get('quantity'))
        except ValueError:
            continue
        if quantity <= 0 or clean(raw.get('unit')).upper() != 'EA':
            continue
        erp = clean(raw.get('erp'))
        if not erp:
            continue
        mapped = mapping.get(erp) or {}
        icube = clean(mapped.get('icube') or raw.get('icube')).upper()
        shown = lookup(icube, raw.get('name'), raw.get('spec'), refs=refs)
        factory = factory_for(icube, display(shown.get('name')), raw, refs)
        bucket, _, _ = stock_bucket(raw, factory, refs)
        target = inventory[erp]
        target['total'] += quantity
        if bucket == 'available':
            target['available'] += quantity
        target['raw'] = raw
        target['icube'] = icube

    codes = shipment_codes | set(inventory)
    rows = []
    for erp in codes:
        stock = inventory.get(erp, {})
        raw = stock.get('raw') or {}
        mapped = mapping.get(erp) or {}
        icube = clean(stock.get('icube') or mapped.get('icube') or raw.get('icube')).upper()
        shown = lookup(icube, raw.get('name') or mapped.get('name'), raw.get('spec'), refs=refs)
        name = display(shown.get('name')) or clean(raw.get('name')) or '품목 매핑 확인'
        factory = factory_for(icube, name, raw, refs)
        averages = {}
        for period, months in windows.items():
            total = sum((monthly.get((erp, month), ZERO) for month in months), ZERO)
            averages[period] = total / len(months) if months else ZERO
        average = averages[selected_period]
        available = stock.get('available', ZERO)
        coverage = available / average if average > 0 else None
        if average <= 0:
            status = '출고 없음'
        elif coverage < 1:
            status = '1개월 미만'
        elif coverage < 3:
            status = '3개월 미만'
        else:
            status = '3개월 이상'
        rows.append(dict(
            erp=erp, icube=icube or '미매핑', name=name,
            type=display(shown.get('type')) or '-', size=display(shown.get('size')) or '-',
            category=display(shown.get('category')) or '확인 필요',
            factory=factory, available=available, total=stock.get('total', ZERO),
            avg3=averages[3], avg6=averages[6], avg12=averages[12],
            average=average, coverage=coverage, status=status,
            mapped=bool(icube and shown.get('mapped')),
        ))

    query = clean(request.args.get('q')).casefold()
    selected_factory = clean(request.args.get('product_factory'))
    selected_status = clean(request.args.get('coverage_status'))
    if selected_factory not in {'12', '3', 'unknown'}:
        selected_factory = ''
    valid_statuses = {'1개월 미만', '3개월 미만', '3개월 이상', '출고 없음'}
    if selected_status not in valid_statuses:
        selected_status = ''
    filtered = []
    for row in rows:
        if query and not any(query in clean(row[key]).casefold()
                             for key in ('erp', 'icube', 'name', 'type', 'size', 'category')):
            continue
        if selected_factory and row['factory'] != selected_factory:
            continue
        if selected_status and row['status'] != selected_status:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: (
        0 if row['coverage'] is not None else 1,
        row['coverage'] if row['coverage'] is not None else Decimal('999999'),
        natural(row['name']), natural(row['size']), row['erp']))
    total_available = sum((row['available'] for row in filtered), ZERO)
    total_average = sum((row['average'] for row in filtered if row['average'] > 0), ZERO)
    total_coverage = total_available / total_average if total_average > 0 else None
    status_counts = Counter(row['status'] for row in filtered)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Seoul')).date()
    return dict(
        rows=filtered, row_count=len(filtered), all_count=len(rows),
        total_available=total_available, total_average=total_average,
        total_coverage=total_coverage, selected_period=selected_period,
        window_months=windows[selected_period], windows=windows,
        earliest=earliest, latest=latest, completed_end=completed_end,
        negative_rows=negative_rows,
        future_rows=sum(day > today for day, _, _ in parsed),
        status_counts=status_counts,
        unmapped=sum(not row['mapped'] for row in filtered),
        filters=dict(q=clean(request.args.get('q')), product_factory=selected_factory,
                     coverage_status=selected_status),
    )


def install_shipment_analysis(app, db):
    if 'expiry.shipment_analysis' in app.view_functions:
        return
    models = app.extensions['expiry_models']
    Reference, Import = models['Reference'], models['Import']

    def refs():
        result = {}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def latest(kind):
        return db.session.scalar(select(Import).where(
            Import.kind == kind, Import.committed_at.is_not(None)
        ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))

    def shipment_source():
        saved = latest('shipment')
        return (saved.payload, saved) if saved is not None else (seed_rows(), None)

    @login_required
    def analysis_view():
        if request.method == 'POST':
            if current_user.role not in {'admin', 'editor'}:
                abort(403)
            upload = request.files.get('shipment_file')
            if upload is None or not upload.filename:
                flash('출고현황 파일을 선택해 주세요.', 'error')
                return redirect(url_for('expiry.shipment_analysis'))
            raw = upload.stream.read(MAX_FILE_SIZE + 1)
            if len(raw) > MAX_FILE_SIZE:
                flash('출고현황 파일은 4MB 이하만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.shipment_analysis'))
            try:
                rows, negative = parse_shipments(raw.decode('utf-8-sig'), upload.filename)
            except UnicodeDecodeError:
                flash('UTF-8로 저장된 파일만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.shipment_analysis'))
            except ValueError as error:
                flash(str(error), 'error')
                return redirect(url_for('expiry.shipment_analysis'))
            as_of = max(date.fromisoformat(row['date']) for row in rows)
            db.session.execute(text('SELECT pg_advisory_xact_lock(73190423)'))
            record = Import(
                id=str(uuid.uuid4()), kind='shipment', payload=rows,
                notes={'filename': upload.filename, 'rows': len(rows), 'negative_rows': negative},
                base_revision='shipment-v1', as_of=as_of,
                created_by=current_user.id, committed_at=datetime.now(timezone.utc))
            db.session.add(record)
            db.session.commit()
            app.logger.info('SHIPMENT_DATA_UPDATED user=%s rows=%s', current_user.id, len(rows))
            flash(f'출고현황 {len(rows):,}개 일자·품번 합산행을 적용했습니다.', 'success')
            return redirect(url_for('expiry.shipment_analysis'))
        try:
            period = int(request.args.get('period', 6))
        except ValueError:
            period = 6
        if period not in {3, 6, 12}:
            period = 6
        shipment_rows, shipment_import = shipment_source()
        stock = latest('stock')
        board = build_analysis(shipment_rows, stock.payload if stock else [], refs(), period)
        return render_template('shipment_analysis.html', board=board, shipment_import=shipment_import,
                               stock=stock, can_edit=current_user.role in {'admin', 'editor'})

    @login_required
    def shipment_download():
        rows, _ = shipment_source()
        return Response('\ufeff' + shipment_markdown(rows),
                        mimetype='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename=shipment_history.md'})

    app.add_url_rule('/expiry/shipment-analysis', endpoint='expiry.shipment_analysis',
                     view_func=analysis_view, methods=['GET', 'POST'])
    app.add_url_rule('/expiry/shipment-analysis/download', endpoint='expiry.shipment_download',
                     view_func=shipment_download, methods=['GET'])
