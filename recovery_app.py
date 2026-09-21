"""Recovery entrypoint: restore missing URLs, then verify signed-in GET rendering.

Checks use isolated internal request contexts and read-only DB transactions.
They do not add an account, issue an external session, or expose inventory data.
"""
import json
import uuid
from types import SimpleNamespace

from flask import g, render_template_string
from sqlalchemy import text

from app import app, db, audit, roles
from expiry_route_restore import restore_routes
from inventory_spec_view import install_inventory_spec_view

restore_routes(app, db, audit, roles)
install_inventory_spec_view(app, db)


@app.errorhandler(500)
def safe_server_error(error):
    error_id = uuid.uuid4().hex[:12]
    original = getattr(error, 'original_exception', None)
    if original is not None:
        app.logger.error('Request failure reference=%s', error_id,
                         exc_info=(type(original), original, original.__traceback__))
    db.session.rollback()
    return render_template_string(
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<title>Request error</title><body style="font-family:sans-serif;padding:40px">'
        '<h1>요청을 처리하지 못했습니다.</h1>'
        '<p>오류 확인 번호: {{ error_id }}</p>'
        '<a href="/expiry/dashboard">재고 대시보드</a>'
        '</body></html>', error_id=error_id), 500


def verify_authenticated_get_views():
    checks = [
        ('/expiry/dashboard', 'expiry.dashboard', {}),
        ('/expiry/dashboard?show_value=1', 'expiry.dashboard', {}),
        ('/expiry/product-order', 'expiry.product_order', {}),
        ('/expiry/unit-costs', 'expiry.unit_costs', {}),
        ('/expiry/', 'expiry.index', {}),
        ('/expiry/inventory-location', 'expiry.inventory_location', {}),
        ('/expiry/master-data', 'expiry.master_data', {}),
        ('/expiry/stock-history', 'expiry.stock_history', {}),
        ('/expiry/analysis', 'expiry.analysis', {}),
        ('/expiry/warehouse-classes', 'expiry.warehouse_classes', {}),
        ('/expiry/references/mapping', 'expiry.reference_list', {'kind': 'mapping'}),
        ('/expiry/import/stock', 'expiry.import_data', {'kind': 'stock'}),
        ('/expiry/dashboard-expiry', 'expiry.dashboard_expiry', {}),
    ]
    passed, failures = 0, []
    for path, endpoint, values in checks:
        with app.test_request_context(path, method='GET', base_url='https://localhost'):
            g._login_user = SimpleNamespace(
                is_authenticated=True, is_active=True, is_anonymous=False,
                role='admin', name='Internal render check', id='render-check-not-an-account',
                must_change_password=False,
            )
            try:
                db.session.execute(text('SET TRANSACTION READ ONLY'))
                response = app.make_response(app.view_functions[endpoint](**values))
                if response.status_code != 200:
                    raise RuntimeError(f'{endpoint}: expected 200, got {response.status_code}')
                body = response.get_data(as_text=True)
                if 'Internal Server Error' in body or '<title>Request error</title>' in body:
                    raise RuntimeError(f'{endpoint}: error document returned')
                if endpoint == 'expiry.dashboard':
                    if 'data-release="inventory-specs-20260921-02"' not in body:
                        raise RuntimeError('Dashboard release marker missing')
                    if '규격(사이즈)' not in body:
                        raise RuntimeError('Specification column missing')
                    if '기타가용' not in body or '완제품' not in body or '공정중' not in body:
                        raise RuntimeError('Per-spec quantity columns missing')
                    if 'iv-spec-qty-chips' not in body:
                        raise RuntimeError('Per-spec quantity summary missing')
                    if '<details class="product-stock-group"' not in body:
                        raise RuntimeError('Dashboard rendered without product stock groups')
                passed += 1
                print(f'RECOVERY_RENDER_OK {path} status=200', flush=True)
            except Exception as error:
                failures.append({'path': path, 'error_type': type(error).__name__})
                app.logger.exception('RECOVERY_RENDER_FAILED %s', path)
            finally:
                db.session.rollback()
    return {'passed': passed, 'total': len(checks), 'failures': failures}


def verify_anonymous_protection():
    paths = ['/expiry/dashboard', '/expiry/product-order', '/expiry/unit-costs',
             '/expiry/', '/expiry/master-data']
    passed = 0
    with app.test_client() as client:
        for path in paths:
            response = client.get(path, base_url='https://localhost', follow_redirects=False)
            if response.status_code == 302 and '/login' in response.headers.get('Location', ''):
                passed += 1
            else:
                app.logger.error('RECOVERY_AUTH_CHECK_FAILED %s status=%s', path, response.status_code)
    return {'passed': passed, 'total': len(paths)}


app.add_url_rule('/health/inventory-specs-20260921-02',
                 endpoint='inventory_spec_readiness',
                 view_func=lambda: health_with_render_verification(), methods=['GET'])

app.config['RECOVERY_RENDER_REPORT'] = verify_authenticated_get_views()
app.config['RECOVERY_AUTH_REPORT'] = verify_anonymous_protection()
_original_health = app.view_functions['health']


def health_with_render_verification():
    response = app.make_response(_original_health())
    result = response.get_json(silent=True) or {}
    render = app.config['RECOVERY_RENDER_REPORT']
    auth = app.config['RECOVERY_AUTH_REPORT']
    passed = render['passed'] == render['total'] and auth['passed'] == auth['total']
    result.update(recovery_checks_passed=passed,
                  authenticated_page_checks=render['passed'],
                  authenticated_page_checks_total=render['total'],
                  anonymous_access_checks=auth['passed'],
                  recovery_revision='inventory-specs-20260921-02',
                  render_failures=render['failures'])
    if not passed:
        result.update(status='error', application_ready=False)
        response.status_code = 503
    response.set_data(json.dumps(result, ensure_ascii=True))
    response.mimetype = 'application/json'
    return response


app.view_functions['health'] = health_with_render_verification
