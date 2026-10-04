"""Small standard-library CDP client for the Chrome validation suite (no extra dependencies)."""
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import urlopen


class DevTools:
    def __init__(self, url):
        parsed = urlparse(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall(f'GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'.encode())
        headers = b''
        while not headers.endswith(b'\r\n\r\n'):
            headers += self.socket.recv(1)
        if b' 101 ' not in headers:
            raise RuntimeError('DevTools websocket handshake failed')
        self.sequence = 0

    def exact(self, length):
        data = b''
        while len(data) < length:
            chunk = self.socket.recv(length - len(data))
            if not chunk:
                raise EOFError('DevTools disconnected')
            data += chunk
        return data

    def call(self, method, params=None):
        self.sequence += 1
        payload = json.dumps({'id': self.sequence, 'method': method, 'params': params or {}}).encode()
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x81, 0x80 | length]) if length < 126 else bytes([0x81, 0xfe]) + struct.pack('!H', length)
        if length > 65535:
            header = bytes([0x81, 0xff]) + struct.pack('!Q', length)
        self.socket.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
        fragments = b''
        while True:
            first, second = self.exact(2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self.exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self.exact(8))[0]
            frame = self.exact(length)
            if first & 15 == 8:
                raise EOFError('DevTools closed')
            if first & 15 not in (0, 1):
                continue
            fragments += frame
            if not first & 0x80:
                continue
            result = json.loads(fragments)
            fragments = b''
            if result.get('id') == self.sequence:
                if 'error' in result:
                    raise RuntimeError(result['error'])
                return result.get('result', {})

    def evaluate(self, expression):
        if expression.lstrip().startswith('(async'):
            expression = 'await ' + expression
        result = self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True, 'replMode': True})
        if 'exceptionDetails' in result:
            raise AssertionError(result['exceptionDetails'])
        return result.get('result', {}).get('value')

    def wait(self, expression, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(.05)
        diagnostic = self.evaluate("document.getElementById('offline-message')?.textContent || ''")
        raise AssertionError('Browser condition timed out: ' + expression + '; UI: ' + str(diagnostic))


class Chrome:
    def __init__(self, executable, profile):
        self.executable, self.profile = executable, profile

    def start(self):
        port_file = Path(self.profile) / 'DevToolsActivePort'
        if port_file.exists():
            port_file.unlink()  # Only our own temporary profile file.
        startup = None
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        self.process = subprocess.Popen([self.executable, '--headless=new', '--disable-gpu', '--no-first-run',
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', f'--user-data-dir={self.profile}', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup)
        deadline = time.monotonic() + 15
        while not port_file.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Chrome did not start')
            time.sleep(.05)
        self.port = int(port_file.read_text().splitlines()[0])
        with urlopen(f'http://127.0.0.1:{self.port}/json/version', timeout=10) as response:
            self.browser = DevTools(json.load(response)['webSocketDebuggerUrl'])
        return self

    def tab(self):
        target = self.browser.call('Target.createTarget', {'url': 'about:blank'})['targetId']
        with urlopen(f'http://127.0.0.1:{self.port}/json/list', timeout=10) as response:
            tabs = json.load(response)
        tab = DevTools(next(t['webSocketDebuggerUrl'] for t in tabs if t['id'] == target))
        tab.call('Page.enable')
        return tab

    def stop(self):
        try:
            self.browser.call('Browser.close')
        except (EOFError, OSError):
            pass
        self.browser.socket.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
