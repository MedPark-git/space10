import json
import stat
from concurrent.futures import ThreadPoolExecutor

from runtime_security import private_settings, remove_bootstrap_credentials


def test_private_settings_persists_one_secret_across_workers(tmp_path):
    created=[]
    def factory():
        created.append(1)
        return {'value':'local-test-only'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda _:private_settings('test.json',factory,tmp_path),range(8)))
    assert values==[{'value':'local-test-only'}]*8 and len(created)==1
    assert stat.S_IMODE((tmp_path/'test.json').stat().st_mode)==0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode)==0o700


def test_bootstrap_secret_removed_only_after_correct_user_changes_password(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNTIME_PRIVATE_DIR',str(tmp_path))
    private_settings('bootstrap-admin.json',lambda:{'login_id':'admin','password':'isolated-test-only'})
    remove_bootstrap_credentials('other')
    assert (tmp_path/'bootstrap-admin.json').exists()
    remove_bootstrap_credentials('admin')
    assert not (tmp_path/'bootstrap-admin.json').exists()
