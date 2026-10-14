"""Exact Mac kernel identities for the admitted private native controllers."""
import ctypes
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading

from .manager import ManagementError

_helper = None
_lock = threading.Lock()


class _ProcBSDInfo(ctypes.Structure):
    # Darwin sys/proc_info.h: struct proc_bsdinfo (PROC_PIDTBSDINFO).
    _fields_ = [('_flags', ctypes.c_uint32 * 3), ('pid', ctypes.c_uint32),
                ('ppid', ctypes.c_uint32), ('uid', ctypes.c_uint32),
                ('_other_ids', ctypes.c_uint32 * 6), ('_names', ctypes.c_char * 48),
                ('_process', ctypes.c_uint32 * 6), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


_libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
_libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
_libproc.proc_pidinfo.restype = ctypes.c_int
_libproc.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
_libproc.proc_pidpath.restype = ctypes.c_int


def controller_helper():
    """Build the small peer-PID reader outside the native model's writable roots."""
    global _helper
    with _lock:
        if _helper is None:
            # Darwin's per-user temp directory is independent of model TMPDIR.
            parent = Path(os.confstr(65537)).resolve(strict=True)
            directory = Path(tempfile.mkdtemp(prefix='hermes-controller-', dir=parent))
            directory.chmod(0o700)
            source = directory / 'trusted_controller.c'
            source.write_bytes(Path(__file__).with_suffix('.c').read_bytes())
            source.chmod(0o400)
            executable = directory / 'trusted-controller'
            result = subprocess.run(['/usr/bin/cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                     str(source), '-o', str(executable)], capture_output=True, check=False)
            if result.returncode:
                raise ManagementError('configuration_missing', 'The original Mac kernel peer identity reader could not be built.')
            executable.chmod(0o500)
            _helper = str(executable)
        return _helper


def validate_controller_helper(executable, workspace, home):
    """Refuse helper paths reachable through the original Bash writable grants."""
    candidate = Path(executable)
    try:
        resolved = candidate.resolve(strict=True)
        allowed = Path(os.confstr(65537)).resolve(strict=True)
        if resolved != candidate or not candidate.is_relative_to(allowed):
            raise ValueError
        for protected in (candidate.parent, candidate):
            info = protected.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or stat.S_ISLNK(info.st_mode):
                raise ValueError
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError
        roots = (Path(workspace).resolve(), Path('/private/tmp'), Path(home).resolve() / 'tmp')
        if any(candidate.is_relative_to(root) or root.is_relative_to(candidate.parent) for root in roots):
            raise ValueError
    except (OSError, ValueError):
        raise ManagementError('binding_conflict', 'The kernel identity reader is not outside native model writable roots.') from None
    return str(candidate)


def controller_identity(pid=None):
    """Return PID, exact kernel birth and executable; no request claims are used."""
    if pid is None:
        # Prepare the host's reader once, before this gateway dispatches workers.
        controller_helper()
        pid = os.getpid()
    if type(pid) is not int or pid <= 0:
        raise ManagementError('binding_conflict', 'A live exact controller process is required.')
    info = _ProcBSDInfo()
    executable = ctypes.create_string_buffer(4096)
    if (_libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
            or info.pid != pid or info.uid != os.getuid()
            or _libproc.proc_pidpath(pid, executable, len(executable)) <= 0):
        raise ManagementError('outcome_unknown', 'The exact controller kernel identity is unavailable.')
    return {'pid': pid, 'start_sec': str(info.start_sec), 'start_usec': str(info.start_usec),
            'executable': os.fsdecode(executable.value)}
