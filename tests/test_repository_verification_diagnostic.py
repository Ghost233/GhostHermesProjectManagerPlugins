"""Untrusted verification output leaves only bounded metadata in diagnostics."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def test_original_host_diagnostic_omits_unknown_credentials_and_preserves_output_hashes():
    stdout = 'password=synthetic-password\nAWS_SECRET_ACCESS_KEY=synthetic-cloud-secret\n'
    stderr = 'other_provider_credential=synthetic-vendor-secret\n'
    script = r"""
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import vm from 'node:vm';
const source = fs.readFileSync(process.argv[1], 'utf8');
const begin = source.indexOf('const verificationDiagnostic = value => {');
const end = source.indexOf('\nasync function verifyTests', begin);
if (begin < 0 || end < 0) throw new Error('Production diagnostic function is unavailable.');
let written;
vm.runInNewContext(source.slice(begin, end) + '\nverificationDiagnostic(input);', {
  config: {dsh_home: '/fixture/home'}, process: {env: {}}, path, crypto, Buffer,
  fs: {writeFileSync: (target, raw, options) => {written = {value: JSON.parse(raw), mode: options.mode};}},
  input: JSON.parse(fs.readFileSync(0, 'utf8')),
});
process.stdout.write(JSON.stringify(written));
"""
    host = Path(__file__).resolve().parents[1] / 'ghost_hermes_pm/owned_persistent_host.mjs'
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, str(host)],
        input=json.dumps({'exitCode': 1, 'stdout': {'text': stdout, 'truncated': False},
                          'stderr': {'text': stderr, 'truncated': False},
                          'type': 'Error', 'message': 'password=synthetic-message-secret'}),
        capture_output=True, text=True, check=True)
    written = json.loads(result.stdout)
    raw = json.dumps(written)
    for secret in ('synthetic-password', 'synthetic-cloud-secret', 'synthetic-vendor-secret', 'synthetic-message-secret'):
        assert secret not in raw
    assert written['mode'] == 0o600
    for name, text in (('stdout', stdout), ('stderr', stderr)):
        assert written['value'][name]['sha256'] == hashlib.sha256(text.encode()).hexdigest()
        assert written['value'][name]['bytes'] == len(text.encode())
