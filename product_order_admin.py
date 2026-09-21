"""Editable product order and factory assignment for inventory/expiry dashboards."""
from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from inventory_spec_view import clean, factory_for, natural, num
from product_display_master import lookup


def install_product_order_admin(app, db):
    if app.config.get('PRODUCT_ORDER_ADMIN_INSTALLED'):
        return
    app.config['PRODUCT_ORDER_ADMIN_INSTALLED'] = True
    models = app.extensions['expiry_models']
    Reference, Import = models['Reference'], models['Import']

    def references():
        result = {}
        for row in db.session.scalars(select(Reference).order_by(Reference.kind, Reference.key)):
            result.setdefault(row.kind, {})[row.key] = row.payload
        return result

    def product_names(snapshot, refs):
        products = {}
        mapping = refs.get('mapping', {})
        for entry in snapshot.payload or []:
            raw = entry.get('data', {}) if isinstance(entry, dict) else {}
            if not isinstance(raw, dict) or clean(raw.get('account')) != '제품':
                continue
            try:
                if num(raw.get('quantity')) <= 0:
                    continue
            except ValueError:
                continue
            erp = clean(raw.get('erp'))
            mapped = mapping.get(erp) or {}
            code = clean(mapped.get('icube') or raw.get('icube')).upper()
            shown = lookup(code, raw.get('name'), raw.get('spec'), refs=refs)
            name = clean(shown.get('name') or raw.get('name')) or '제품명 미등록'
            products[name] = factory_for(code, name, raw, refs)
        return products

    @login_required
    def product_order_view():
        if current_user.role not in {'admin', 'editor'}:
            abort(403)
        refs = references()
        snapshot = db.session.scalar(select(Import).where(
            Import.kind == 'stock', Import.committed_at.is_not(None)
        ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))
        detected = product_names(snapshot, refs) if snapshot is not None else {}
        saved = refs.get('product_order', {}).get('dashboard', {})
        if not isinstance(saved, dict):
            saved = {}

        if request.method == 'POST':
            names = [clean(value) for value in request.form.getlist('product_name')]
            factories = request.form.getlist('product_factory')
            positions = request.form.getlist('product_position')
            if len(names) != len(factories) or len(names) != len(positions) or set(names) != set(detected) or len(names) != len(set(names)):
                abort(400)
            items = []
            for index, (name, factory, position) in enumerate(zip(names, factories, positions)):
                if factory not in {'12', '3'}:
                    abort(400)
                try:
                    rank = max(1, int(position))
                except ValueError:
                    abort(400)
                items.append((factory, rank, index, name))
            order12 = [item[3] for item in sorted((i for i in items if i[0] == '12'), key=lambda i: (i[1], i[2]))]
            order3 = [item[3] for item in sorted((i for i in items if i[0] == '3'), key=lambda i: (i[1], i[2]))]
            db.session.execute(text('SELECT pg_advisory_xact_lock(73190425)'))
            now = datetime.now(timezone.utc)
            order_row = db.session.scalar(select(Reference).where(Reference.kind == 'product_order', Reference.key == 'dashboard'))
            if order_row is None:
                order_row = Reference(kind='product_order', key='dashboard')
                db.session.add(order_row)
            order_row.payload = {'factory12': order12, 'factory3': order3}
            order_row.updated_by, order_row.updated_at = current_user.id, now
            for factory, _, _, name in items:
                row = db.session.scalar(select(Reference).where(Reference.kind == 'product_factory', Reference.key == name))
                if row is None:
                    row = Reference(kind='product_factory', key=name)
                    db.session.add(row)
                row.payload = {'product': name, 'factory': factory}
                row.updated_by, row.updated_at = current_user.id, now
            db.session.commit()
            app.logger.info('PRODUCT_ORDER_FACTORY_UPDATED user=%s products=%s', current_user.id, len(items))
            flash('제품 순서와 공장 구분을 저장했습니다.', 'success')
            return redirect(url_for('expiry.product_order'))

        ranks12 = {name: index for index, name in enumerate(saved.get('factory12', []))}
        ranks3 = {name: index for index, name in enumerate(saved.get('factory3', []))}
        products12 = [name for name, factory in detected.items() if factory == '12']
        products3 = [name for name, factory in detected.items() if factory == '3']
        products12.sort(key=lambda name: (ranks12.get(name, 10**9), natural(name)))
        products3.sort(key=lambda name: (ranks3.get(name, 10**9), natural(name)))
        return render_template('product_order.html', products12=products12, products3=products3, snapshot=snapshot)

    if 'expiry.product_order' not in app.view_functions:
        raise RuntimeError('Existing product order endpoint is missing')
    app.view_functions['expiry.product_order'] = product_order_view
