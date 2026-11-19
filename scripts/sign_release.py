"""Run only in the protected release environment. Private key never touches disk."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from proficentr.database import MIGRATIONS
from proficentr.updates import PUBLIC_KEY, VERSION, version


def main():
    tag = sys.argv[1]
    if tag != 'v' + VERSION:
        raise ValueError('Release tag must match the application version')
    version(tag)
    if tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'] != VERSION:
        raise ValueError('pyproject version mismatch')
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(os.environ.pop('RELEASE_ED25519_PRIVATE_KEY')))
    if key.public_key().public_bytes_raw().hex() != PUBLIC_KEY:
        raise ValueError('Signing key does not match the embedded public key')
    package = Path('dist') / f'Proficentr-{VERSION}-Windows.zip'
    with package.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    raw = json.dumps({'format': 1, 'version': VERSION, 'filename': package.name, 'size': package.stat().st_size,
                      'sha256': digest, 'platform': 'windows-x86_64', 'min_updater': 1,
                      'schema': len(MIGRATIONS)}, sort_keys=True, separators=(',', ':')).encode()
    Path('dist/manifest.json').write_bytes(raw)
    Path('dist/manifest.sig').write_bytes(key.sign(raw))


if __name__ == '__main__':
    main()
