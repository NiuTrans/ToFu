"""Windows log path handles; retain filesystem ACLs, never emulate POSIX modes.

Entry point ``open_log_path`` uses Win32 no-follow handles for files/directories.
Only stdlib ctypes/msvcrt are loaded, and only when called on Windows. Parent
paths must be trusted: OPEN_REPARSE_POINT protects the final component only.
"""
from __future__ import annotations

import os


def open_log_path(path, flags: int, *, directory: bool = False) -> int:
    """Open a direct disk object, rejecting every final-component reparse point.

    Return an owned CRT descriptor for files; directories are validated and
    closed and return -1. API/access errors propagate; no chmod/ACL rewrite.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                      wintypes.DWORD]
    query.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]

    access = 0x80 if directory else 0x80000000  # attributes / GENERIC_READ
    if not directory and flags & (os.O_WRONLY | os.O_RDWR):
        access = 0x40000000  # GENERIC_WRITE
        if flags & os.O_RDWR:
            access |= 0x80000000
    disposition = 3  # OPEN_EXISTING
    if flags & os.O_CREAT:
        disposition = 1 if flags & os.O_EXCL else 4  # CREATE_NEW / OPEN_ALWAYS
    handle = create(os.fspath(path), access, 7, None, disposition,
                    0x00200000 | 0x02000000, None)  # no-follow + backup semantics
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeTag()
        if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400:
            raise OSError('log target is a reparse point: %s' % path)
        if bool(info.attributes & 0x10) != directory:
            raise OSError('log target has wrong file type: %s' % path)
        if directory:
            return -1
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (flags & (
            os.O_WRONLY | os.O_RDWR | os.O_APPEND)))
        handle = None  # CRT descriptor now owns the native handle.
        return descriptor
    finally:
        if handle is not None:
            close(handle)
