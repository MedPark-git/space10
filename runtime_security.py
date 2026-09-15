"""Private, persistent runtime credentials. Nothing is written into the source tree."""
import fcntl
import json
import os
from pathlib import Path
import secrets


def private_settings(filename, factory, directory=None):
    directory = Path(directory or os.getenv('RUNTIME_PRIVATE_DIR', '/app/user_data/private'))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock_fd = os.open(directory / '.credentials.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        path = directory / filename
        if path.exists():
            os.chmod(path, 0o600)
            return json.loads(path.read_text())
        value = factory()
        temporary = directory / (filename + '.' + secrets.token_hex(8) + '.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return value
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def session_secret():
    return private_settings('session.json', lambda: {'secret_key': secrets.token_hex(32)})['secret_key']


def bootstrap_credentials():
    return private_settings('bootstrap-admin.json', lambda: {
        'login_id': os.getenv('BOOTSTRAP_ADMIN_ID', 'admin').casefold(),
        'password': secrets.token_urlsafe(24),
    })


def remove_bootstrap_credentials(login_id):
    path = Path(os.getenv('RUNTIME_PRIVATE_DIR', '/app/user_data/private')) / 'bootstrap-admin.json'
    try:
        credentials = json.loads(path.read_text())
        if credentials.get('login_id') == login_id:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
