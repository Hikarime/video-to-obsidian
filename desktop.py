"""Open collection files and bring their Obsidian window forward on Windows."""

from __future__ import annotations

import os
from pathlib import PureWindowsPath
import time
from urllib.parse import quote, urlencode
import webbrowser


class Windows:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.ctypes = ctypes
        self.callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(library, name, result, *arguments):
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
            return function

        self.enum = bind(user, "EnumWindows", w.BOOL, self.callback, w.LPARAM)
        self.visible = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
        self.title = bind(user, "GetWindowTextW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        self.pid = bind(user, "GetWindowThreadProcessId", w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        self.open_process = bind(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        self.image = bind(kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        self.close = bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
        self.minimized = bind(user, "IsIconic", w.BOOL, w.HWND)
        self.show = bind(user, "ShowWindowAsync", w.BOOL, w.HWND, ctypes.c_int)
        self.foreground = bind(user, "SetForegroundWindow", w.BOOL, w.HWND)
        self.style = bind(user, "GetWindowLongW", w.LONG, w.HWND, ctypes.c_int)
        self.position = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)

    def find(self, vault_name):
        from ctypes import wintypes as w

        found = None

        @self.callback
        def visit(window, _):
            nonlocal found
            title = self.ctypes.create_unicode_buffer(1024)
            self.title(window, title, len(title))
            if not self.visible(window) or f" - {vault_name} - obsidian".casefold() not in title.value.casefold():
                return True
            pid = w.DWORD()
            self.pid(window, self.ctypes.byref(pid))
            process = self.open_process(0x1000, False, pid.value)  # Query access only.
            if process:
                try:
                    image = self.ctypes.create_unicode_buffer(32768)
                    size = w.DWORD(len(image))
                    if self.image(process, 0, image, self.ctypes.byref(size)) and PureWindowsPath(image.value).name.casefold() == "obsidian.exe":
                        found = window
                        return False
                finally:
                    self.close(process)
            return True

        self.enum(visit, 0)
        return found

    def activate(self, window):
        if self.minimized(window):
            self.show(window, 9)  # SW_RESTORE; leave maximized windows unchanged.
        if not self.foreground(window) and not self.style(window, -20) & 0x8:
            # Raise above the browser, then immediately restore normal stacking.
            flags = 0x4000 | 0x40 | 0x10 | 0x2 | 0x1
            self.position(window, -1, 0, 0, 0, 0, flags)
            self.position(window, -2, 0, 0, 0, 0, flags)
            self.foreground(window)


def open_gallery(document, vault_name):
    uri = "obsidian://open?" + urlencode({"path": str(document)}, quote_via=quote)
    if os.name != "nt":
        webbrowser.open(uri)
        return
    os.startfile(uri)
    windows = Windows()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        window = windows.find(vault_name)
        if window:
            windows.activate(window)
            return
        time.sleep(.1)
