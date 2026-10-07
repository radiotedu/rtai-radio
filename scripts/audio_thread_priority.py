"""Set the broadcaster's Windows priority and keep audio I/O one level above it."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os


def set_broadcast_process_priority() -> dict[str, object]:
    if os.name != "nt":
        return {"applied": False, "reason": "not_windows"}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    handle = kernel.GetCurrentProcess()
    previous = kernel.GetPriorityClass(handle)
    # Match video and text encoders. Preserve a higher user-specified priority.
    if previous in {0x8000, 0x80, 0x100}:
        return {"applied": True, "previous_priority": previous, "priority": previous}
    applied = bool(kernel.SetPriorityClass(handle, 0x8000))
    return {"applied": applied, "previous_priority": previous,
            "priority": kernel.GetPriorityClass(handle),
            "error": None if applied else ctypes.get_last_error()}


def set_audio_thread_priority(process_id: int | None = None, thread_id: int | None = None) -> dict[str, object]:
    if os.name != "nt":
        return {"applied": False, "reason": "not_windows"}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
    kernel.GetThreadPriority.restype = ctypes.c_int
    kernel.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel.SetThreadPriority.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenThread(0x60, False, thread_id) if thread_id is not None else kernel.GetCurrentThread()
    if not handle:
        return {"applied": False, "error": ctypes.get_last_error()}
    try:
        owner = kernel.GetProcessIdOfThread(handle)
        if process_id is not None and owner != process_id:
            return {"applied": False, "reason": "thread_owner_changed", "process_id": owner}
        previous = kernel.GetThreadPriority(handle)
        applied = bool(kernel.SetThreadPriority(handle, 1))  # THREAD_PRIORITY_ABOVE_NORMAL
        return {"applied": applied, "process_id": owner, "thread_id": thread_id,
                "previous_priority": previous, "priority": kernel.GetThreadPriority(handle)}
    finally:
        if thread_id is not None:
            kernel.CloseHandle(handle)
