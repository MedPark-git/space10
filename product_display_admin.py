"""Editable product display master stored in the existing reference table."""
import csv
import io
import re
from datetime import datetime, timezone

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from product_display_data import DATA

HEADERS = ('품번', '앞', '중간', '뒤', '품명', '규격', '제품명', '타입', '사이즈', '분류')
KEYS = ('code', 'front', 'middle', 'suffix', 'item_name', 'spec', 'name', 'type', 'size', 'category')
REQUIRED = ('code', 'name', 'type', 'size', 'category')
MAX_FILE_SIZE = 2 * 1024 * 1024


def _clean(value):
    return str(value or '').strip().replace(r'\*', '*').replace(r'\~', '~')


def _markdown_cells(line):
    body = line.strip()
    if body.startswith('|'):
        body = body[1:]
    if body.endswith('|'):
        body = body[:-1]
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '|':
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    current.append('\\' if escaped else '')
    cells.append(''.join(current).strip())
    return cells


def parse_master(text, filename=''):
    text = text.lstrip('\ufeff')
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError('파일이 비어 있습니다.')
    if lines[0].lstrip().startswith('|'):
        raw_rows = [_markdown_cells(line) for line in lines]
    else:
        delimiter = '\t' if '\t' in lines[0] or filename.lower().endswith('.tsv') else ','
        raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header = [_clean(value) for value in raw_rows[0]]
    if header != list(HEADERS):
        raise ValueError('첫 행의 열 순서가 품번·앞·중간·뒤·품명·규격·제품명·타입·사이즈·분류와 일치해야 합니다.')
    items, errors = {}, []
    for line_no, values in enumerate(raw_rows[1:], 2):
        if len(values) == 10 and all(re.fullmatch(r':?-{2,}:?', _clean(v)) for v in values):
            continue
        if len(values) != 10:
            errors.append(f'{line_no}행: 열이 {len(values)}개입니다(필수 10개).')
            continue
        row = dict(zip(KEYS, (_clean(value) for value in values)))
        row['code'] = row['code'].upper()
        missing = [HEADERS[KEYS.index(key)] for key in REQUIRED if not row[key]]
        if missing:
            errors.append(f"{line_no}행: {', '.join(missing)} 값이 없습니다.")
            continue
        if row['code'] in items:
            errors.append(f"{line_no}행: 품번 {row['code']}이 중복되었습니다.")
            continue
        code = row.pop('code')
        items[code] = row
    if errors:
        more = f' 외 {len(errors)-10}건' if len(errors) > 10 else ''
        raise ValueError(' / '.join(errors[:10]) + more)
    if not items:
        raise ValueError('적용할 품목이 없습니다.')
    if len(items) > 5000:
        raise ValueError('품목 수가 5,000건을 초과합니다.')
    return items


def current_items(refs):
    payload = refs.get('product_display', {}).get('master', {})
    items = payload.get('items') if isinstance(payload, dict) else None
    return (items, payload) if isinstance(items, dict) and items else (DATA, {})


def markdown_file(items):
    output = ['| ' + ' | '.join(HEADERS) + ' |', '| ' + ' | '.join('---' for _ in HEADERS) + ' |']
    for code, row in items.items():
        values = [code] + [row.get(key, '') for key in KEYS[1:]]
        escaped = [str(value).replace('\\', '\\\\').replace('|', r'\|') for value in values]
        output.append('| ' + ' | '.join(escaped) + ' |')
    return '\n'.join(output) + '\n'


def register_product_display_admin(bp, db, audit, roles, Reference, references, lock_writes):
    @bp.route('/product-display-master', methods=['GET', 'POST'])
    @roles('admin', 'editor')
    def product_display_master():
        if request.method == 'POST':
            upload = request.files.get('master_file')
            if upload is None or not upload.filename:
                flash('수정한 기준표 파일을 선택해 주세요.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            raw = upload.stream.read(MAX_FILE_SIZE + 1)
            if len(raw) > MAX_FILE_SIZE:
                flash('기준표 파일은 2MB 이하만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            try:
                text = raw.decode('utf-8-sig')
                items = parse_master(text, upload.filename)
            except UnicodeDecodeError:
                flash('UTF-8로 저장된 파일만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            except ValueError as error:
                flash(str(error), 'error')
                return redirect(url_for('expiry.product_display_master'))
            lock_writes()
            row = db.session.scalar(select(Reference).where(
                Reference.kind == 'product_display', Reference.key == 'master'))
            if row is None:
                row = Reference(kind='product_display', key='master')
                db.session.add(row)
            row.payload = {
                'items': items,
                'count': len(items),
                'filename': upload.filename,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            row.updated_by = current_user.id
            row.updated_at = datetime.now(timezone.utc)
            audit('product_display_master_updated', detail=f'{len(items)} items', commit=False)
            db.session.commit()
            flash(f'제품 표시 기준표 {len(items):,}건을 적용했습니다.', 'success')
            return redirect(url_for('expiry.product_display_master'))
        refs = references()
        items, payload = current_items(refs)
        preview = [dict(code=code, **row) for code, row in list(items.items())[:30]]
        return render_template('product_display_master.html', count=len(items), preview=preview,
                               source=payload.get('filename') or '현재 배포 기준표',
                               updated_at=payload.get('updated_at'))

    @bp.get('/product-display-master/download')
    @roles('admin', 'editor')
    def product_display_master_download():
        items, _ = current_items(references())
        return Response(
            '\ufeff' + markdown_file(items),
            mimetype='text/markdown; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename=product_display_master.md'},
        )


def install_product_display_admin(app, db):
    """Install only the isolated product-master routes; leave existing expiry routes untouched."""
    if 'expiry.product_display_master' in app.view_functions:
        return
    Reference = app.extensions['expiry_models']['Reference']

    def references():
        result = {}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def require_editor():
        if current_user.role not in {'admin', 'editor'}:
            abort(403)

    @login_required
    def master_view():
        require_editor()
        if request.method == 'POST':
            upload = request.files.get('master_file')
            if upload is None or not upload.filename:
                flash('수정한 기준표 파일을 선택해 주세요.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            raw = upload.stream.read(MAX_FILE_SIZE + 1)
            if len(raw) > MAX_FILE_SIZE:
                flash('기준표 파일은 2MB 이하만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            try:
                items = parse_master(raw.decode('utf-8-sig'), upload.filename)
            except UnicodeDecodeError:
                flash('UTF-8로 저장된 파일만 등록할 수 있습니다.', 'error')
                return redirect(url_for('expiry.product_display_master'))
            except ValueError as error:
                flash(str(error), 'error')
                return redirect(url_for('expiry.product_display_master'))
            db.session.execute(text('SELECT pg_advisory_xact_lock(73190422)'))
            row = db.session.scalar(select(Reference).where(
                Reference.kind == 'product_display', Reference.key == 'master'))
            if row is None:
                row = Reference(kind='product_display', key='master')
                db.session.add(row)
            row.payload = {
                'items': items,
                'count': len(items),
                'filename': upload.filename,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            row.updated_by = current_user.id
            row.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            app.logger.info('PRODUCT_DISPLAY_MASTER_UPDATED user=%s items=%s', current_user.id, len(items))
            flash(f'제품 표시 기준표 {len(items):,}건을 적용했습니다.', 'success')
            return redirect(url_for('expiry.product_display_master'))
        items, payload = current_items(references())
        preview = [dict(code=code, **row) for code, row in list(items.items())[:30]]
        return render_template('product_display_master.html', count=len(items), preview=preview,
                               source=payload.get('filename') or '현재 배포 기준표',
                               updated_at=payload.get('updated_at'))

    @login_required
    def master_download():
        require_editor()
        items, _ = current_items(references())
        return Response(
            '\ufeff' + markdown_file(items),
            mimetype='text/markdown; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename=product_display_master.md'},
        )

    app.add_url_rule('/expiry/product-display-master',
                     endpoint='expiry.product_display_master',
                     view_func=master_view, methods=['GET', 'POST'])
    app.add_url_rule('/expiry/product-display-master/download',
                     endpoint='expiry.product_display_master_download',
                     view_func=master_download, methods=['GET'])
