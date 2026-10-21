"""Recorded providers with per-test sockets and joined handler ownership."""
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest


@contextmanager
def provider(responses):
    """Serve a finite SSE conversation; unexpected calls fail immediately."""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.connection.settimeout(20)
            index = len(requests)
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if index >= len(responses):
                self.send_error(500, 'Unexpected extra model request')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for frame in [
                {'choices':[{'delta':{'role':'assistant','content':json.dumps(responses[index])},
                             'index':0,'finish_reason':None}]},
                {'choices':[{'delta':{},'index':0,'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}},
            ]:
                self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    worker = threading.Thread(target=server.serve_forever)
    try:
        worker.start()
    except BaseException:
        server.server_close()
        raise
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def recorded_provider():
    """Close every provider even when later profile or SDK construction fails."""
    with ExitStack() as owners:
        yield lambda responses:owners.enter_context(provider(responses))
