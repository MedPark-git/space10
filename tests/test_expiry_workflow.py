"""Isolated persistence/HTTP tests. PostgreSQL locking is checked after deployment."""
from datetime import datetime, timezone
import pytest
from sqlalchemy import event, select

from app import User, app as default_app, create_app, db


@pytest.fixture
def web():
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                      'SQLALCHEMY_ENGINE_OPTIONS': {}, 'SECRET_KEY': 'isolated-test-session',
                      'WTF_CSRF_ENABLED': False, 'SESSION_COOKIE_SECURE': False})
    with app.app_context():
        @event.listens_for(db.engine, 'connect')
        def lock_stub(connection, _):
            connection.create_function('pg_advisory_xact_lock', 1, lambda value: 1)
        db.create_all()
        for role in ['admin', 'editor', 'viewer']:
            user = User(id=role, login_id=role, name=role, role=role, must_change_password=False)
            user.set_password('isolated-password-29')
            db.session.add(user)
        db.session.commit()
        yield app, app.test_client()
        db.session.remove()
        db.drop_all()


def login(client, user='editor'):
    from flask import g
    g.pop('_login_user', None)
    with client.session_transaction() as session:
        session['_user_id'] = user
        session['_fresh'] = True


def preview(client, kind, data, as_of=None):
    response = client.post('/expiry/import/'+kind, data={'data':data,'as_of':as_of or ''})
    assert response.status_code == 302
    return response.location.rsplit('/',1)[-1]


def commit(client, import_id):
    response=client.post('/expiry/commit/'+import_id)
    assert response.status_code == 302
    return response


def test_reference_upsert_preserves_absent_keys_and_records_previous_values(web):
    app,client=web;login(client)
    first=preview(client,'mapping','아마란스 품번\tICUBE 품번\n0001\t11BC025-01\n0002\t11BC050-01')
    assert client.get('/expiry/preview/'+first).status_code==200
    commit(client,first)
    second=preview(client,'mapping','아마란스 품번\tICUBE 품번\n0001\t11BC100-01')
    response=client.get('/expiry/preview/'+second)
    assert b'11BC025-01' in response.data and b'11BC100-01' in response.data
    commit(client,second)
    Reference=app.extensions['expiry_models']['Reference']
    rows=db.session.scalars(select(Reference)).all()
    assert len(rows)==2 and {r.key:r.payload['icube'] for r in rows}=={'0001':'11BC100-01','0002':'11BC050-01'}


def test_snapshot_preview_commit_idempotency_and_routes(web):
    app,client=web;login(client)
    commit(client,preview(client,'mapping','아마란스 품번\tICUBE 품번\n0001\t11BC025-01'))
    commit(client,preview(client,'rules','KEY값\tKEY값2\t유효기간\n11BC\t01\t3'))
    pending=preview(client,'stock','창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\t재고단위\n창고\tA\t0001\t=HYPERLINK(1)\t제품\tXB240229C1001\t1.25\tEA','2026-09-15')
    Import=app.extensions['expiry_models']['Import']
    assert db.session.get(Import,pending).committed_at is None
    assert client.get('/expiry/preview/'+pending).status_code==200
    commit(client,pending); timestamp=db.session.get(Import,pending).committed_at
    commit(client,pending)
    assert db.session.get(Import,pending).committed_at==timestamp
    for url in ['/expiry/','/expiry/?q=0001','/expiry/import/mts','/expiry/import/exceptions','/expiry/references/mapping',
                '/expiry/references/rules','/expiry/alerts']:
        assert client.get(url).status_code==200, url
    response=client.get('/expiry/?as_of=2027-02-27')
    assert '오늘 만료'.encode() in response.data and b'1.25' in response.data
    exported=client.get('/expiry/export.csv').get_data(as_text=True)
    assert "'=HYPERLINK(1)" in exported
    assert db.session.scalar(select(db.func.count()).select_from(__import__('app').Item))==0


def test_reference_change_invalidates_pending_preview(web):
    app,client=web;login(client)
    a=preview(client,'mapping','아마란스 품번\tICUBE 품번\n0001\t11BC025-01')
    b=preview(client,'mapping','아마란스 품번\tICUBE 품번\n0002\t11BC050-01')
    commit(client,b)
    response=commit(client,a)
    assert response.location.endswith('/expiry/import/mapping')
    Import=app.extensions['expiry_models']['Import']
    assert db.session.get(Import,a).committed_at is None


def test_viewer_cannot_mutate_and_other_editor_cannot_commit_preview(web):
    app,client=web
    assert client.get('/expiry/').status_code==302
    login(client)
    pending=preview(client,'mapping','아마란스 품번\tICUBE 품번\n0001\t11BC025-01')
    login(client,'admin')
    assert client.get('/expiry/preview/'+pending).status_code==403
    assert client.post('/expiry/commit/'+pending).status_code==403
    login(client,'viewer')
    assert client.get('/expiry/').status_code==200
    assert client.get('/expiry/references/mapping').status_code==200
    assert client.post('/expiry/import/mapping',data={'data':'bad'}).status_code==403
    assert client.post('/expiry/alerts',data={'day1':'10','day2':'20','day3':'30'}).status_code==403


def test_csrf_and_alert_validation(web):
    app,client=web;login(client)
    response=client.post('/expiry/alerts',data={'day1':'90','day2':'90','day3':'365'})
    assert response.status_code==200
    assert db.session.scalar(select(db.func.count()).select_from(app.extensions['expiry_models']['Reference']))==0
    response=client.post('/expiry/alerts',data={'day1':'30','day2':'90','day3':'180'})
    assert response.status_code==302
    app.config['WTF_CSRF_ENABLED']=True
    assert client.post('/expiry/import/mapping',data={'data':'bad'}).status_code==400


def test_unauthenticated_api_returns_json_and_ui_requires_login(web):
    _, client = web
    response = client.get('/api/items')
    assert response.status_code == 401 and response.json == {'error': 'authentication_required'}
    assert client.get('/expiry/').status_code == 302


def test_admin_delivery_route_is_closed_after_handoff(web, tmp_path, monkeypatch):
    from runtime_security import private_settings
    _, client = web
    monkeypatch.setenv('RUNTIME_PRIVATE_DIR', str(tmp_path))
    monkeypatch.setenv('BOOTSTRAP_DELIVERY_PUBLIC_KEY', 'obsolete-deployment-config')
    private_settings('bootstrap-admin.json', lambda: {'login_id':'admin', 'password':'isolated-password-29'})
    user = db.session.get(User, 'admin'); user.must_change_password = True; db.session.commit()
    assert client.get('/setup/bootstrap-envelope').status_code == 404
    assert client.get('/setup/bootstrap-envelope?verification=closed').status_code == 404


def test_finished_product_scope_applies_to_preview_dashboard_csv_and_old_snapshots(web):
    app, client = web; login(client)
    source = ('창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\t재고단위\n'
              '부적합창고\tA\tP1\tPRODUCT_VISIBLE\t제품\t240039SA\t2.5\tEA\n'
              '부적합창고\tA\tS1\tSEMI_EXCLUDED\t반제품\tBAD\t100\tEA\n'
              '다른창고\tA\tU1\tUNKNOWN_EXCLUDED\t\tBAD\t300\tEA')
    pending = preview(client, 'stock', source, '2026-09-16')
    html = client.get('/expiry/preview/'+pending).get_data(as_text=True)
    assert 'PRODUCT_VISIBLE' in html and 'SEMI_EXCLUDED' not in html and 'UNKNOWN_EXCLUDED' not in html
    commit(client, pending)
    Import = app.extensions['expiry_models']['Import']
    assert len(db.session.get(Import, pending).payload) == 3  # Preserve original input for audit.
    for url in ['/expiry/', '/expiry/?zero=1', '/expiry/export.csv']:
        body = client.get(url).get_data(as_text=True)
        assert 'PRODUCT_VISIBLE' in body and 'SEMI_EXCLUDED' not in body and 'UNKNOWN_EXCLUDED' not in body
    exported = client.get('/expiry/export.csv').get_data(as_text=True)
    assert '계정구분,LOT' in exported
    saved = db.session.get(Import, pending)
    saved.payload = [{'key':'legacy', 'data':{k:v for k,v in saved.payload[0]['data'].items() if k != 'account'}}]
    db.session.commit()
    html = client.get('/expiry/').get_data(as_text=True)
    assert '계정구분 열을 포함해 재고를 다시 등록' in html and 'PRODUCT_VISIBLE' not in html
    assert 'PRODUCT_VISIBLE' not in client.get('/expiry/export.csv').get_data(as_text=True)


def test_factory_3_suffix_and_common_rules_can_be_registered_together(web):
    app, client = web; login(client)
    source = ('공장\t앞자리 KEY\t끝자리 KEY\t유효일수\tMTS 제품구분\n'
              '3\t39FD\t*\t1095\tS GEN\n3\t39FD\t12\t1095\tHAHA GEN')
    pending = preview(client, 'rules', source)
    commit(client, pending)
    Reference = app.extensions['expiry_models']['Reference']
    assert {r.key for r in db.session.scalars(select(Reference))} == {'39FD|*', '39FD|12'}
    response = client.post('/expiry/import/rules', data={'data':'공장\t앞자리 KEY\t끝자리 KEY\t유효일수\tMTS 제품구분\n1·2\t39FD\t01\t1095\t'})
    assert response.status_code == 200 and '충돌'.encode() in response.data


def test_nonproduct_only_snapshot_replaces_previous_product_view_without_showing_semifinished(web):
    _, client = web; login(client)
    source = '창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\nA\tB\t1\tOLD_PRODUCT\t제품\tBAD\t1'
    commit(client, preview(client, 'stock', source, '2026-09-15'))
    source = source.replace('OLD_PRODUCT', 'SEMI_ONLY').replace('제품', '반제품')
    commit(client, preview(client, 'stock', source, '2026-09-16'))
    html = client.get('/expiry/').get_data(as_text=True)
    assert 'OLD_PRODUCT' not in html and 'SEMI_ONLY' not in html
    assert '조회 조건에 맞는 재고가 없습니다' in html


def family_preview(client, prefix='42AP', days='730', enabled='1', reason='확정된 변경 사유'):
    import re
    html = client.get('/expiry/special-rules').get_data(as_text=True)
    revision = re.search(r'name="revision" value="([a-f0-9]+)"', html).group(1)
    response = client.post('/expiry/special-rules/preview', data={
        'prefix':prefix, 'days':days, 'enabled':enabled, 'reason':reason, 'revision':revision})
    assert response.status_code == 302
    return response.location.rsplit('/',1)[-1]


def test_special_rule_preview_commit_history_disable_and_audit(web):
    from app import AuditLog
    app, client = web; login(client)
    commit(client, preview(client, 'mapping', '아마란스 품번\tICUBE 품번\nP1\t42AP100-99\nP2\t42AP100-01\nS1\t42AP100-12'))
    source = ('창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\n'
              'A\tB\tP1\tAP 제품\t제품\tSA240229P101\t2\n'
              'A\tB\tP2\tLOT 확인대상\t제품\tBAD\t1\n'
              'A\tB\tS1\t반제품\t반제품\tSA240229P101\t99\n'
              'A\tB\tU1\t품번미등록\t제품\tSA240229P101\t1')
    commit(client, preview(client, 'stock', source, '2026-09-16'))
    pending_id = family_preview(client)
    Import = app.extensions['expiry_models']['Import']; Reference = app.extensions['expiry_models']['Reference']
    pending = db.session.get(Import, pending_id)
    assert pending.committed_at is None and pending.notes['before']['days'] == 1095
    assert pending.notes['impact']['targets'] == 2 and pending.notes['impact']['date_changes'] == 1
    assert pending.notes['impact']['unresolved_before'] == pending.notes['impact']['unresolved_after'] == 1
    assert pending.notes['impact']['unmapped_products'] == 1
    assert db.session.scalar(select(Reference).where(Reference.kind=='family_rules')) is None
    body = client.get('/expiry/special-rules/preview/'+pending_id).get_data(as_text=True)
    assert '2027-02-27' in body and '2026-02-27' in body and '확인 후 적용 확정' in body
    assert '대상 계열 여부를 판단할 수 없습니다' in body
    commit(client, pending_id); commit(client, pending_id)
    saved = db.session.scalar(select(Reference).where(Reference.kind=='family_rules', Reference.key=='42AP'))
    assert saved.payload['days'] == 730 and saved.payload['enabled'] is True
    logs = list(db.session.scalars(select(AuditLog).where(AuditLog.event=='expiry_special_rule_updated')))
    assert len(logs) == 1 and '1095' in logs[0].detail and '730' in logs[0].detail
    body = client.get('/expiry/special-rules?edit=42AP').get_data(as_text=True)
    assert '확정된 변경 사유' in body and 'editor' in body and 'readonly' in body
    disabled = family_preview(client, enabled='0', reason='일반 규칙으로 복귀')
    commit(client, disabled)
    body = client.get('/expiry/').get_data(as_text=True)
    assert '규칙 미등록' in body and '확인 필요 사유' in body
    saved = db.session.scalar(select(Reference).where(Reference.kind=='family_rules', Reference.key=='42AP'))
    assert saved.payload['enabled'] is False


@pytest.mark.parametrize('change', ['references', 'stock'])
def test_special_rule_confirmation_blocks_stale_reference_or_latest_stock(web, change):
    app, client = web; login(client)
    pending = family_preview(client, prefix='99AB')
    if change == 'references':
        commit(client, preview(client, 'mapping', '아마란스 품번\tICUBE 품번\nP1\t99AB100-01'))
    else:
        commit(client, preview(client, 'stock', '창고\t장소\t품번\t품명\t계정구분\tLOT No.\t기말재고\nA\tB\tP1\t제품\t제품\tSA240229P101\t1', '2026-09-17'))
    html = client.get('/expiry/special-rules/preview/'+pending).get_data(as_text=True)
    assert '확인 후 적용 확정' not in html and '다시 확인' in html
    assert commit(client, pending).location.endswith('/expiry/special-rules')
    Import = app.extensions['expiry_models']['Import']
    assert db.session.get(Import, pending).committed_at is None


def test_special_rules_role_owner_csrf_and_new_rule_without_inventory(web):
    app, client = web
    assert client.get('/expiry/special-rules').status_code == 302
    login(client, 'viewer')
    html = client.get('/expiry/special-rules').get_data(as_text=True)
    assert '42AP' in html and '49AP' in html and '영향 미리보기' not in html
    assert client.post('/expiry/special-rules/preview',data={}).status_code == 403
    login(client)
    pending = family_preview(client, prefix='99AB')
    login(client,'admin')
    assert client.get('/expiry/special-rules/preview/'+pending).status_code == 403
    assert client.post('/expiry/commit/'+pending).status_code == 403
    login(client)
    assert '아직 확정된 재고가 없어' in client.get('/expiry/special-rules/preview/'+pending).get_data(as_text=True)
    commit(client,pending)
    assert '99AB' in client.get('/expiry/special-rules').get_data(as_text=True)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/expiry/special-rules/preview',data={}).status_code == 400
