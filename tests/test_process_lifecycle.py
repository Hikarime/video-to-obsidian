from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode


PROJECT = Path(__file__).resolve().parents[1]
APP_SCRIPT = PROJECT / "app.py"


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        output = PROJECT / ".test-output" / "lifecycle"
        output.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(self.temp.cleanup)
        vault = Path(self.temp.name)
        (vault / ".obsidian").mkdir()
        root = vault / "Inbox"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0))
            self.port = listener.getsockname()[1]
        if self.port > 65515:
            self.skipTest("Ephemeral port is outside the tool's configurable range")
        executable = Path(sys.executable)
        gui_python = executable.with_name("pythonw.exe")
        if os.name == "nt" and gui_python.is_file():
            executable = gui_python
        self.process = subprocess.Popen(
            [str(executable),"-B",str(APP_SCRIPT),"--inbox",str(root),"--port",str(self.port),"--no-browser"],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.addCleanup(self.cleanup_process)
        deadline = time.monotonic()+6
        while time.monotonic()<deadline:
            try:
                status,body = self.request("GET","/")
                if status == 200:
                    self.config = json.loads(re.search(r"const config = (\{[^\n]+\});",body.decode()).group(1))
                    break
            except OSError:
                pass
            if self.process.poll() is not None:
                self.fail("Importer exited before serving the page")
            time.sleep(.025)
        else:
            self.fail("Importer did not become ready")
        self.streams = []
        self.addCleanup(self.close_streams)

    def request(self,method,path,payload=None):
        connection = http.client.HTTPConnection("127.0.0.1",self.port,timeout=2)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method,path,body=body,headers={"Content-Type":"application/json"})
            response = connection.getresponse()
            return response.status,response.read()
        finally:
            connection.close()

    def page(self,client_id=None):
        client_id = client_id or self.config["client"]
        connection = http.client.HTTPConnection("127.0.0.1",self.port,timeout=2)
        connection.request("GET","/api/session?"+urlencode({"client":client_id,"token":self.config["token"]}))
        response = connection.getresponse()
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers.get("Content-Type"),"text/event-stream")
        self.assertEqual(response.readline(),b"retry: 200\n")
        while response.readline() != b"\n":
            pass
        stream = connection,response
        self.streams.append(stream)
        return stream

    def close_stream(self,stream):
        connection,response = stream
        response.close()
        connection.close()
        self.streams.remove(stream)

    def close_streams(self):
        for stream in list(self.streams):
            self.close_stream(stream)

    def cleanup_process(self):
        if self.process.poll() is None:
            # Only the exact child created by this test is ever terminated.
            self.process.terminate()
            self.process.wait(timeout=3)

    def assert_exited(self,timeout=4):
        started = time.monotonic()
        self.assertEqual(self.process.wait(timeout=timeout),0)
        self.assertLess(time.monotonic()-started,timeout)
        with socket.socket() as connection:
            connection.settimeout(.3)
            self.assertNotEqual(connection.connect_ex(("127.0.0.1",self.port)),0)

    def test_exit_button_ends_real_pythonw_process_and_releases_port(self):
        self.page()
        status,_ = self.request("POST","/api/shutdown",{"token":self.config["token"]})
        self.assertEqual(status,200)
        self.assert_exited(timeout=2)

    def test_tab_close_beacon_ends_real_process(self):
        stream = self.page()
        status,_ = self.request("POST","/api/client-close",{"client":self.config["client"],"token":self.config["token"]})
        self.assertEqual(status,200)
        self.close_stream(stream)
        self.assert_exited()

    def test_browser_disconnect_without_beacon_also_ends_real_process(self):
        stream = self.page()
        self.close_stream(stream)
        self.assert_exited()

    def test_refresh_and_background_tab_do_not_leave_or_prematurely_stop_process(self):
        first = self.page()
        self.request("POST","/api/client-close",{"client":self.config["client"],"token":self.config["token"]})
        self.close_stream(first)
        refreshed = self.page("a"*32)
        other = self.page("b"*32)
        self.close_stream(refreshed)
        # No JS polling is needed to keep a background page open.
        time.sleep(2.1)
        self.assertIsNone(self.process.poll())
        self.close_stream(other)
        self.assert_exited()


if __name__ == "__main__":
    unittest.main()
