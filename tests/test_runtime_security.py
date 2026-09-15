import json
import stat
import base64
import pytest
from concurrent.futures import ThreadPoolExecutor

from runtime_security import private_settings, remove_bootstrap_credentials
from runtime_security import bootstrap_envelope


def test_bootstrap_envelope_requires_owner_private_key():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    credentials = {'login_id': 'admin', 'password': 'isolated-test-only'}
    envelope = bootstrap_envelope(credentials, public)
    assert 'password' not in json.dumps(envelope) and credentials['password'] not in json.dumps(envelope)
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    ciphertext = base64.b64decode(envelope['ciphertext'])
    assert json.loads(key.decrypt(ciphertext, oaep)) == credentials
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    with pytest.raises(ValueError):
        wrong_key.decrypt(ciphertext, oaep)
    with pytest.raises(ValueError):
        bootstrap_envelope(credentials, 'invalid-key')


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
