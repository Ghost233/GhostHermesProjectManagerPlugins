"""Complete source fingerprints for the explicitly selected official SDK fixture."""
import hashlib
import os
from pathlib import Path
import stat


def source_snapshot(root):
    root = Path(root).resolve(strict=True)
    result = {}
    def unreadable(error):
        raise error
    for directory, children, files in os.walk(root, followlinks=False, onerror=unreadable):
        children[:] = sorted(name for name in children if name not in {'.git', '__pycache__'})
        aliases = [name for name in children if (Path(directory) / name).is_symlink()]
        for name in sorted([*files, *aliases]):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if name == '.env' or name.startswith('.env.') and not name.endswith('.example'):
                raise ValueError('The SDK fixture contains a non-source environment file; its bytes were not read.')
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                data = os.fsencode(os.readlink(path))
                kind = 'symlink'
            elif stat.S_ISREG(metadata.st_mode):
                data = path.read_bytes()
                kind = 'file'
            else:
                raise ValueError('The SDK fixture contains a non-source special file.')
            result[relative] = (kind, stat.S_IMODE(metadata.st_mode), hashlib.sha256(data).hexdigest())
    anchor = '@deepseek-ai/dsh/lib/profile-boot.js'
    if (root / anchor).is_file() and anchor not in result:
        raise ValueError('The selected DSH SDK inventory omitted its readable original profile entry.')
    return result
