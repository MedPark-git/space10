"""Restore routes lost by a truncated source update, without replacing stock data.

The surviving expiry view supplies the original query/calculation helpers.
No schema, credentials, inventory rows or reference rows are changed at startup.
"""
import hashlib
import inspect
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, text

from expiry_engine import FORMATS, InputError, calculate, index_mts, iso_date, number, parse_paste, stock_scope, summarize

BANDS = [
    ('expired', '\ub9cc\ub8cc'), ('due90', '1~90\uc77c'),
    ('due180', '91~180\uc77c'), ('due365', '181~365\uc77c'),
    ('safe', '365\uc77c \ucd08\uacfc'), ('issue', '\ud655\uc778 \ud544\uc694'),
]


def restore_routes(app, db, audit, roles):
    models = app.extensions['expiry_models']
    Reference, Import = models['Reference'], models['Import']
    original = inspect.unwrap(app.view_functions['expiry.dashboard_v2'])
    helpers = inspect.getclosurevars(original).nonlocals
    current_view = helpers['current_view']
    stat = helpers['inventory_stat']
    bucket = helpers['action_bucket']
    product_groups = helpers['inventory_product_groups']
    warehouse_class = helpers['warehouse_classification']
    availability_stats = helpers['availability_stats']
    references = inspect.getclosurevars(current_view).nonlocals['references']

    def now():
        return datetime.now(timezone.utc)

    def today():
        return now().astimezone(ZoneInfo('Asia/Seoul')).date()

    def revision(refs):
        return hashlib.sha256(json.dumps(refs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def lock_writes():
        db.session.execute(text('SELECT pg_advisory_xact_lock(73190422)'))

    def latest():
        return db.session.scalar(select(Import).where(
            Import.kind == 'stock', Import.committed_at.is_not(None)
        ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(1))

    def history():
        return db.session.scalars(select(Import).where(
            Import.kind == 'stock', Import.committed_at.is_not(None)
        ).order_by(Import.as_of.desc(), Import.committed_at.desc()).limit(100)).all()

    def paginate(rows):
        pages = max(1, (len(rows) + 99) // 100)
        page = min(max(1, request.args.get('page', 1, type=int)), pages)
        return rows[(page - 1)*100:page*100], page, pages

    def page_links(endpoint, page, pages):
        args = request.args.to_dict(flat=False)
        args.pop('page', None)
        return {
            'previous_url': url_for(endpoint, **args, page=max(1, page - 1)),
            'next_url': url_for(endpoint, **args, page=min(pages, page + 1)),
        }

    def distinct_lots(rows):
        return len({(r.get('erp'), r.get('lot') or ('missing', i)) for i, r in enumerate(rows)})

    def quality(rows):
        negative = [r for r in rows if number(r['quantity']) < 0]
        non_ea = [r for r in rows if number(r['quantity']) > 0 and str(r.get('unit') or '').upper() != 'EA']
        return {
            'negative': {'quantity': abs(sum((number(r['quantity']) for r in negative), number(0))), 'lots': distinct_lots(negative)},
            'non_ea_lots': distinct_lots(non_ea), 'non_ea': [],
        }

    def band_stats(rows):
        return [{'key': key, 'label': label, **stat([r for r in rows if bucket(r) == label])} for key, label in BANDS]

    @login_required
    def index():
        refs, snapshot, as_of, summary, warehouses, statuses, rows = current_view()
        shown, page, pages = paginate(rows)
        selected = request.args.get('bucket', '')
        labels = dict(BANDS + [('all_ea', '\uc804\uccb4 \uc591\uc218 EA \uc7ac\uace0'),
                              ('negative', '\uc74c\uc218\uc7ac\uace0'), ('non_ea', 'EA \uc678 \uc7ac\uace0')])
        return render_template('expiry.html', snapshot=snapshot, as_of=as_of,
            rows=shown, total=len(rows), page=page, pages=pages, summary=summary,
            warehouses=warehouses, statuses=statuses, history=history(), formats=FORMATS,
            ref_counts={k: len(refs.get(k, {})) for k in FORMATS}, inventory_total=stat(rows),
            inventory_buckets=band_stats(rows), quality=quality(rows),
            selected_bucket=selected, selected_bucket_label=labels.get(selected),
            export_url=url_for('expiry.export', **request.args.to_dict(flat=False)),
            **page_links('expiry.index', page, pages))

    @login_required
    def inventory_location():
        refs, snapshot, as_of, _, warehouses, _, rows = current_view()
        labels = {'available': '\uac00\uc6a9\uc7ac\uace0', 'unavailable': '\ube44\uac00\uc6a9\uc7ac\uace0',
                  'unclassified': '\ubd84\ub958 \ud544\uc694'}
        positive = [dict(r) for r in rows if number(r['quantity']) > 0]
        summary = availability_stats(positive, refs)
        for row in positive:
            row['availability'], row['availability_source'] = warehouse_class(row.get('warehouse'), refs)
            row['availability_label'] = labels[row['availability']]
        selected = request.args.get('availability', '')
        if selected and selected not in labels:
            abort(400)
        filtered = [r for r in positive if not selected or r['availability'] == selected]
        shown, page, pages = paginate(filtered)
        return render_template('inventory_location.html', snapshot=snapshot, as_of=as_of,
            rows=shown, total=len(filtered), page=page, pages=pages,
            groups=product_groups(filtered, refs)[:100], summary=summary,
            warehouses=warehouses, selected=selected, labels=labels,
            **page_links('expiry.inventory_location', page, pages))

    @login_required
    def master_data():
        refs = references()
        kinds = [(k, FORMATS[k][0], len(refs.get(k, {}))) for k in FORMATS if k != 'stock']
        return render_template('expiry_master_data.html', kinds=kinds,
                               family_rule_count=len(refs.get('family_rules', {})))

    @login_required
    def stock_history():
        return render_template('expiry_stock_history.html', imports=history(), latest=latest())

    @login_required
    def reference_list(kind):
        if kind not in FORMATS or kind == 'stock':
            abort(404)
        refs = references()
        q = request.args.get('q', '').strip()
        rows = [v for k, v in sorted(refs.get(kind, {}).items())
                if not q or q.casefold() in (k + json.dumps(v, ensure_ascii=False)).casefold()]
        shown, page, pages = paginate(rows)
        return render_template('expiry_references.html', title=FORMATS[kind][0], kind=kind,
            rows=shown, total=len(rows), page=page, pages=pages, q=q, formats=FORMATS)

    @roles('admin', 'editor')
    def warehouse_classes():
        if request.method == 'POST':
            names = request.form.getlist('warehouse_name')
            classes = request.form.getlist('classification')
            if len(names) != len(classes) or any(not n.strip() for n in names):
                abort(400)
            if any(c not in {'available', 'unavailable', 'unclassified'} for c in classes):
                abort(400)
            lock_writes()
            import re
            for name, classification in zip(names, classes):
                name = name.strip()
                key = re.sub(r'\s+', '', name).casefold()
                row = db.session.scalar(select(Reference).where(Reference.kind == 'warehouse_classes', Reference.key == key))
                if classification == 'unclassified':
                    if row is not None:
                        db.session.delete(row)
                    continue
                if row is None:
                    row = Reference(kind='warehouse_classes', key=key)
                    db.session.add(row)
                row.payload = {'name': name, 'classification': classification}
                row.updated_by, row.updated_at = current_user.id, now()
            audit('warehouse_classes_updated', detail=f'{len(names)} warehouses', commit=False)
            db.session.commit()
            flash('\ucc3d\uace0 \ubd84\ub958\ub97c \uc800\uc7a5\ud588\uc2b5\ub2c8\ub2e4.', 'success')
            return redirect(url_for('expiry.warehouse_classes'))
        refs, snapshot = references(), latest()
        entries, _ = stock_scope(snapshot.payload if snapshot else [])
        names = sorted({e['data'].get('warehouse') or '\ubbf8\uc9c0\uc815' for e in entries})
        rows = []
        for name in names:
            classification, source = warehouse_class(name, refs)
            rows.append({'name': name, 'classification': classification, 'source': source})
        return render_template('warehouse_classes.html', rows=rows, snapshot=snapshot)

    @login_required
    def analysis():
        _, snapshot, as_of, summary, _, _, rows = current_view()
        active = [r for r in rows if number(r['quantity']) != 0]
        ea = [r for r in active if number(r['quantity']) > 0 and str(r.get('unit') or '').upper() == 'EA']
        def stats_for(group):
            return {'total': stat(group), 'expired': stat([r for r in group if bucket(r) == BANDS[0][1]]),
                    'due90': stat([r for r in group if bucket(r) == BANDS[1][1]]),
                    'due180': stat([r for r in group if bucket(r) == BANDS[2][1]]),
                    'issues': stat([r for r in group if r.get('error')])}
        warehouses = [{'name': w, **stats_for([r for r in ea if (r.get('warehouse') or '\ubbf8\uc9c0\uc815') == w])}
                      for w in sorted({r.get('warehouse') or '\ubbf8\uc9c0\uc815' for r in ea})]
        warehouses.sort(key=lambda w: (w['expired']['quantity'], w['due90']['quantity'], w['total']['quantity']), reverse=True)
        factories = [{'label': f, **stat([r for r in ea if (r.get('factory') or '\ubbf8\ud655\uc778') == f])}
                     for f in sorted({r.get('factory') or '\ubbf8\ud655\uc778' for r in ea})]
        issues = [{'label': e, **stat([r for r in ea if r.get('error') == e])} for e in sorted({r['error'] for r in ea if r.get('error')})]
        groups = {}
        for row in active:
            if not row.get('error') and (row.get('remaining') is None or row['remaining'] > 365):
                continue
            key = (row['erp'], row['name'], row.get('spec') or '\uaddc\uaca9 \ubbf8\ub4f1\ub85d', row['unit'])
            g = groups.setdefault(key, dict(erp=key[0], name=key[1], spec=key[2], unit=key[3],
                quantity=number(0), nearest=None, all_lots=set(), expired_lots=set(), due_lots=set(), issue_lots=set()))
            lot = row.get('lot') or (row.get('warehouse'), row.get('location'))
            g['quantity'] += number(row['quantity'])
            g['all_lots'].add(lot)
            if row.get('remaining') is not None:
                g['nearest'] = min(g['nearest'], row['remaining']) if g['nearest'] is not None else row['remaining']
                if row['remaining'] <= 0:
                    g['expired_lots'].add(lot)
                elif row['remaining'] <= 90:
                    g['due_lots'].add(lot)
            if row.get('error'):
                g['issue_lots'].add(lot)
        products = []
        for g in groups.values():
            g.update(lots=len(g.pop('all_lots')), expired=len(g.pop('expired_lots')),
                     due90=len(g.pop('due_lots')), issues=len(g.pop('issue_lots')))
            products.append(g)
        products.sort(key=lambda g: (not g['expired'], not g['due90'], not g['issues'],
                                    g['nearest'] if g['nearest'] is not None else 999999, g['name']))
        return render_template('expiry_analysis_data.html', snapshot=snapshot, as_of=as_of,
            summary=summary, total=len(active), buckets=band_stats(active), warehouses=warehouses,
            products=products[:30], factories=factories, issues=issues, quality=quality(active))

    @roles('admin', 'editor')
    def import_data(kind):
        if kind not in FORMATS:
            abort(404)
        if request.method == 'POST':
            try:
                entries, notes = parse_paste(kind, request.form.get('data', ''))
                as_of = iso_date(request.form.get('as_of')) if kind == 'stock' else None
                refs = references()
                pending = Import(kind=kind, payload=entries, notes=notes,
                                 base_revision=revision(refs), as_of=as_of, created_by=current_user.id)
                db.session.add(pending)
                db.session.commit()
                return redirect(url_for('expiry.preview', import_id=pending.id))
            except InputError as exc:
                db.session.rollback()
                flash(str(exc), 'error')
        return render_template('expiry_import.html', kind=kind, title=FORMATS[kind][0],
                               header=FORMATS[kind][1], formats=FORMATS)

    @roles('admin', 'editor')
    def preview(import_id):
        pending = db.get_or_404(Import, import_id)
        if pending.created_by != current_user.id:
            abort(403)
        if pending.kind == 'family_rules':
            return redirect(url_for('expiry.special_rule_preview', import_id=pending.id))
        if pending.kind not in FORMATS:
            abort(404)
        refs = references()
        entries = pending.payload
        scope, summary, quantity = {}, {}, number(0)
        if pending.kind == 'stock':
            entries, scope = stock_scope(pending.payload)
            indexed = index_mts(refs)
            summary = summarize([calculate(e['data'], indexed, pending.as_of) for e in entries])
            quantity = sum((number(e['data']['quantity']) for e in pending.payload), number(0))
        shown, page, pages = paginate(entries)
        changes = [{'key': e['key'], 'before': refs.get(pending.kind, {}).get(e['key']), 'after': e['data']} for e in shown]
        return render_template('expiry_preview.html', pending=pending, title=FORMATS[pending.kind][0],
            total=len(entries), page=page, pages=pages, changes=changes, scope=scope, summary=summary, quantity=quantity)

    @roles('admin', 'editor')
    def commit(import_id):
        lock_writes()
        pending = db.session.scalar(select(Import).where(Import.id == import_id).with_for_update())
        if pending is None:
            abort(404)
        if pending.created_by != current_user.id:
            abort(403)
        if pending.kind not in FORMATS and pending.kind != 'family_rules':
            abort(400)
        if pending.committed_at is not None:
            return redirect(url_for('expiry.dashboard' if pending.kind == 'stock' else 'expiry.master_data'))
        refs = references()
        if pending.base_revision != revision(refs):
            db.session.rollback()
            flash('\uae30\uc900\uc815\ubcf4\uac00 \ubcc0\uacbd\ub418\uc5c8\uc2b5\ub2c8\ub2e4. \ub2e4\uc2dc \ubbf8\ub9ac\ubcf4\uae30 \ud6c4 \ud655\uc815\ud574 \uc8fc\uc138\uc694.', 'error')
            return redirect(url_for('expiry.special_rules') if pending.kind == 'family_rules' else url_for('expiry.import_data', kind=pending.kind))
        if pending.kind == 'family_rules':
            snapshot = latest()
            if (pending.notes or {}).get('snapshot_id') != (snapshot.id if snapshot else None):
                db.session.rollback()
                flash('\uc7ac\uace0\uac00 \ubcc0\uacbd\ub418\uc5c8\uc2b5\ub2c8\ub2e4. \uc601\ud5a5\ub3c4\ub97c \ub2e4\uc2dc \ud655\uc778\ud574 \uc8fc\uc138\uc694.', 'error')
                return redirect(url_for('expiry.special_rules'))
        if pending.kind != 'stock':
            for entry in pending.payload:
                row = db.session.scalar(select(Reference).where(Reference.kind == pending.kind, Reference.key == entry['key']))
                if row is None:
                    row = Reference(kind=pending.kind, key=entry['key'])
                    db.session.add(row)
                row.payload = dict(entry['data'])
                row.updated_by, row.updated_at = current_user.id, now()
        pending.committed_at = now()
        audit('expiry_import_committed', target_type='expiry_import', target_id=pending.id,
              detail=f'{pending.kind}: {len(pending.payload)} entries', commit=False)
        db.session.commit()
        flash(f'{len(pending.payload):,}\uac74\uc744 \ub4f1\ub85d\ud588\uc2b5\ub2c8\ub2e4.', 'success')
        if pending.kind == 'stock':
            return redirect(url_for('expiry.dashboard', snapshot=pending.id))
        if pending.kind == 'family_rules':
            return redirect(url_for('expiry.special_rules'))
        return redirect(url_for('expiry.reference_list', kind=pending.kind))

    recovered = [
        ('/', 'index', index, ['GET']),
        ('/master-data', 'master_data', master_data, ['GET']),
        ('/stock-history', 'stock_history', stock_history, ['GET']),
        ('/analysis', 'analysis', analysis, ['GET']),
        ('/warehouse-classes', 'warehouse_classes', warehouse_classes, ['GET', 'POST']),
        ('/references/<kind>', 'reference_list', reference_list, ['GET']),
        ('/import/<kind>', 'import_data', import_data, ['GET', 'POST']),
        ('/preview/<import_id>', 'preview', preview, ['GET']),
        ('/commit/<import_id>', 'commit', commit, ['POST']),
    ]
    for path, name, fn, methods in recovered:
        endpoint = 'expiry.' + name
        if endpoint not in app.view_functions:
            app.add_url_rule('/expiry' + path, endpoint=endpoint, view_func=fn, methods=methods)
    # This surviving endpoint had the wrong template and undefined `statuses`.
    app.view_functions['expiry.inventory_location'] = inventory_location
    app.logger.info('Restored expiry routes; stock and reference data left intact')
