"""Monthly product-cost paste workflow, isolated from existing expiry routes."""
import csv
import io
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from unit_cost_seed import COSTS, MONTH as SEED_MONTH


def clean(value):
    return str(value or '').strip()


def cost_number(value):
    raw = clean(value).replace(',', '').replace('원', '').replace('₩', '').strip()
    negative = raw.startswith('(') and raw.endswith(')')
    if negative:
        raw = '-' + raw[1:-1].strip()
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError('제품원가가 숫자가 아닙니다.') from exc
    if not result.is_finite():
        raise ValueError('제품원가가 유효한 숫자가 아닙니다.')
    return result


def parse_cost_paste(content):
    content = content.lstrip('\ufeff')
    reader = csv.reader(io.StringIO(content), delimiter='\t' if '\t' in content else ',')
    items, errors = {}, []
    for line_no, cells in enumerate(reader, 1):
        if not cells or not any(clean(cell) for cell in cells):
            continue
        if line_no == 1 and ('품번' in clean(cells[0]) or 'ICUBE' in clean(cells[0]).upper()):
            continue
        if len(cells) < 2:
            errors.append(f'{line_no}행: 품번과 제품원가가 필요합니다.')
            continue
        code = clean(cells[0]).upper()
        try:
            cost = cost_number(cells[1])
        except ValueError as error:
            errors.append(f'{line_no}행: {error}')
            continue
        if not code:
            errors.append(f'{line_no}행: 품번이 없습니다.')
            continue
        if code in items:
            errors.append(f'{line_no}행: 품번 {code}가 중복되었습니다.')
            continue
        items[code] = cost
    if errors:
        more = f' 외 {len(errors)-10}건' if len(errors) > 10 else ''
        raise ValueError(' / '.join(errors[:10]) + more)
    if not items:
        raise ValueError('적용할 제조원가가 없습니다.')
    return items


def cost_history_with_seed(refs):
    result = {}
    for code, value in COSTS.items():
        cost = Decimal(value)
        if cost >= 0:
            result.setdefault(code, []).append((SEED_MONTH, cost))
    for payload in refs.get('unit_cost', {}).values():
        if not isinstance(payload, dict):
            continue
        code = clean(payload.get('icube')).upper()
        month = clean(payload.get('month'))
        if not code or not re.fullmatch(r'\d{4}-\d{2}', month):
            continue
        try:
            cost = cost_number(payload.get('cost'))
        except ValueError:
            continue
        if cost < 0:
            continue
        result.setdefault(code, []).append((month, cost))
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def cost_index_with_seed(refs, target_month):
    result = {}
    for code, values in cost_history_with_seed(refs).items():
        effective = [item for item in values if item[0] <= target_month]
        if effective:
            result[code] = effective[-1]
    return result


def install_unit_cost_admin(app, db):
    if app.config.get('UNIT_COST_ADMIN_INSTALLED'):
        return
    app.config['UNIT_COST_ADMIN_INSTALLED'] = True
    Reference = app.extensions['expiry_models']['Reference']

    def refs():
        result = {}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def selected_month():
        value = clean(request.values.get('month'))
        if not value:
            value = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m')
        if not re.fullmatch(r'\d{4}-\d{2}', value):
            abort(400)
        return value

    @login_required
    def unit_cost_view():
        month = selected_month()
        if request.method == 'POST':
            if current_user.role not in {'admin', 'editor'}:
                abort(403)
            pasted = request.form.get('pasted', '')
            try:
                items = parse_cost_paste(pasted)
            except ValueError as error:
                flash(str(error), 'error')
                return render_template('unit_cost_admin.html', month=month, rows=[], pasted=pasted,
                                       negative_count=0, can_edit=True)
            db.session.execute(text('SELECT pg_advisory_xact_lock(73190424)'))
            now = datetime.now(timezone.utc)
            for code, cost in items.items():
                key = f'{month}|{code}'
                row = db.session.scalar(select(Reference).where(
                    Reference.kind == 'unit_cost', Reference.key == key))
                if row is None:
                    row = Reference(kind='unit_cost', key=key)
                    db.session.add(row)
                row.payload = {'month': month, 'icube': code, 'cost': str(cost)}
                row.updated_by = current_user.id
                row.updated_at = now
            db.session.commit()
            negative = sum(cost < 0 for cost in items.values())
            app.logger.info('UNIT_COSTS_UPDATED user=%s month=%s items=%s negative=%s',
                            current_user.id, month, len(items), negative)
            message = f'{month} 제조원가 {len(items):,}건을 적용했습니다.'
            if negative:
                message += f' 음수 원가 {negative}건은 재고금액 계산에서 제외됩니다.'
            flash(message, 'success')
            return redirect(url_for('expiry.unit_costs', month=month))
        payloads = []
        if month == SEED_MONTH:
            payloads.extend(dict(icube=code, cost=Decimal(value), source='최초 제공 자료')
                            for code, value in COSTS.items())
        overrides = {}
        for payload in refs().get('unit_cost', {}).values():
            if isinstance(payload, dict) and payload.get('month') == month:
                try:
                    overrides[clean(payload.get('icube')).upper()] = cost_number(payload.get('cost'))
                except ValueError:
                    continue
        rows_by_code = {row['icube']: row for row in payloads}
        for code, cost in overrides.items():
            rows_by_code[code] = dict(icube=code, cost=cost, source='화면 업데이트')
        rows = sorted(rows_by_code.values(), key=lambda row: row['icube'])
        return render_template('unit_cost_admin.html', month=month, rows=rows, pasted='',
                               negative_count=sum(row['cost'] < 0 for row in rows),
                               can_edit=current_user.role in {'admin', 'editor'})

    @login_required
    def unit_cost_download():
        month = selected_month()
        rows = []
        if month == SEED_MONTH:
            rows.extend((code, Decimal(value)) for code, value in COSTS.items())
        for payload in refs().get('unit_cost', {}).values():
            if isinstance(payload, dict) and payload.get('month') == month:
                try:
                    rows.append((clean(payload.get('icube')).upper(), cost_number(payload.get('cost'))))
                except ValueError:
                    continue
        merged = dict(rows)
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(['품번', '출고단가(제품원가)'])
        for code, cost in sorted(merged.items()):
            writer.writerow([code, str(cost)])
        return Response('\ufeff' + stream.getvalue(), mimetype='text/tab-separated-values; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename=unit_cost_{month}.tsv'})

    app.view_functions['expiry.unit_costs'] = unit_cost_view
    app.add_url_rule('/expiry/unit-costs/download', endpoint='expiry.unit_cost_download',
                     view_func=unit_cost_download, methods=['GET'])
