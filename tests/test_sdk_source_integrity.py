"""An unreadable source subtree cannot become a partial immutable-SDK receipt."""
import os

import pytest

from sdk_source_integrity import source_snapshot


def test_source_inventory_reports_directory_read_failure(tmp_path):
    if os.getuid() == 0:
        pytest.skip('Directory permission denial requires an unprivileged owner.')
    package = tmp_path / 'protected-package'
    package.mkdir()
    (package / 'source.js').write_text('export const original = true;\n')
    package.chmod(0)
    try:
        with pytest.raises(PermissionError):
            source_snapshot(tmp_path)
    finally:
        package.chmod(0o700)
    assert source_snapshot(tmp_path)['protected-package/source.js'][0] == 'file'
