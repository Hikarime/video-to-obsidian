from pathlib import Path
import ctypes
from ctypes import wintypes
from unittest import TestCase, main
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import desktop


class DesktopTests(TestCase):
    def test_only_the_matching_vault_in_the_real_obsidian_process_is_selected(self):
        windows = object.__new__(desktop.Windows)
        windows.ctypes = ctypes
        windows.callback = lambda callback: callback
        candidates = {
            1: ("Gallery - Vault - Obsidian 1.14", r"C:\Other\Browser.exe"),
            2: ("Gallery - Other Vault - Obsidian 1.14", r"C:\Apps\Obsidian.exe"),
            3: ("Gallery - Vault - Obsidian 1.14", r"C:\Apps\Obsidian.exe"),
        }
        def enumerate_windows(callback, _):
            for handle in candidates:
                if not callback(handle, 0):
                    break
        def process_id(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = handle
        windows.enum = enumerate_windows
        windows.visible = lambda handle: True
        windows.title = lambda handle, buffer, size: setattr(buffer, "value", candidates[handle][0])
        windows.pid = process_id
        windows.open_process = lambda access, inherit, pid: pid
        def image(process, flags, buffer, size):
            buffer.value = candidates[process][1]
            return True
        windows.image = image
        windows.close = Mock()
        self.assertEqual(windows.find("Vault"), 3)
        self.assertEqual([call.args[0] for call in windows.close.call_args_list], [1, 3])

    def test_windows_opens_encoded_path_and_activates_the_target_vault(self):
        document = Path("Vault with spaces/Inbox/youtube/Gallery.base")
        windows = Mock()
        windows.find.side_effect = [None, 123]
        with patch("desktop.os.name", "nt"), patch("desktop.os.startfile", create=True) as launch, \
                patch("desktop.Windows", return_value=windows), patch("desktop.time.sleep"):
            desktop.open_gallery(document, "Vault with spaces")
        uri = urlsplit(launch.call_args.args[0])
        self.assertEqual((uri.scheme, uri.netloc), ("obsidian", "open"))
        self.assertEqual(parse_qs(uri.query), {"path": [str(document)]})
        windows.find.assert_called_with("Vault with spaces")
        windows.activate.assert_called_once_with(123)

    def test_other_platforms_use_the_registered_protocol_handler(self):
        document = Path("Vault/Gallery.base")
        with patch("desktop.os.name", "posix"), patch("desktop.webbrowser.open") as launch, patch("desktop.Windows") as windows:
            desktop.open_gallery(document, "Vault")
        self.assertTrue(launch.call_args.args[0].startswith("obsidian://open?"))
        windows.assert_not_called()

    def test_restore_minimized_window_without_unmaximizing_an_open_window(self):
        for minimized in (False, True):
            with self.subTest(minimized=minimized):
                windows = Mock(minimized=Mock(return_value=minimized), foreground=Mock(return_value=True))
                desktop.Windows.activate(windows, 123)
                if minimized:
                    windows.show.assert_called_once_with(123, 9)
                else:
                    windows.show.assert_not_called()
                windows.position.assert_not_called()

    def test_foreground_restriction_raises_window_without_leaving_it_topmost(self):
        windows = Mock(minimized=Mock(return_value=False), foreground=Mock(return_value=False), style=Mock(return_value=0))
        desktop.Windows.activate(windows, 123)
        self.assertEqual([call.args[1] for call in windows.position.call_args_list], [-1, -2])

    def test_existing_topmost_setting_is_preserved(self):
        windows = Mock(minimized=Mock(return_value=False), foreground=Mock(return_value=False), style=Mock(return_value=8))
        desktop.Windows.activate(windows, 123)
        windows.position.assert_not_called()


if __name__ == "__main__":
    main()
