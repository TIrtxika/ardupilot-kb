#!/usr/bin/env python3
"""
Persistent KB serve daemon — keeps the reranker (and NLI judge, if used) loaded across requests,
so the relevance-gate and semantic audit are cheap (their cost is one-time model load, not per
query). Quality knobs default ON here (unlike the one-shot CLI where load dominates).

Run:    .kb/venv/bin/python3 .kb/serve/daemon.py
Query:  curl -s localhost:8765/ask -d '{"q":"How does RTL work in ArduCopter?"}'
Health: curl -s localhost:8765/health
Env:    KB_PORT (8765), KB_RERANK (1), KB_SEMANTIC_AUDIT (1), KB_SEMANTIC_JUDGE (llm), KB_GEN_MODEL.
"""
import os, sys, json, re
from http.server import BaseHTTPRequestHandler, HTTPServer

# Quality features default ON in the daemon (model loads are amortized across requests).
os.environ.setdefault('KB_RERANK', '1')
os.environ.setdefault('KB_SEMANTIC_AUDIT', '1')

sys.path.insert(0, '/home/o0rt/Projects/homek/ArduPilot/.kb/serve')
import ask

PORT = int(os.environ.get('KB_PORT', '8765'))


def _prewarm():
    # Load the reranker (and NLI judge if selected) once at startup so the first real query is fast.
    if os.environ.get('KB_RERANK') == '1':
        try:
            ask._rerank_gate('warmup', [(0.0, 'warmup.cpp', 1, 1, 'warmup text for the reranker')])
            print('  reranker loaded', flush=True)
        except Exception as e:
            print('  reranker warmup failed:', e, flush=True)
    if os.environ.get('KB_SEMANTIC_JUDGE', 'llm').lower() == 'nli':
        try:
            ask.semantic_judge_nli('warmup claim', 'warmup context')
            print('  NLI judge loaded', flush=True)
        except Exception as e:
            print('  NLI warmup failed:', e, flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200 if self.path == '/health' else 404,
                   {'ok': True} if self.path == '/health' else {'error': 'POST /ask {"q": ...}'})

    def do_POST(self):
        if self.path != '/ask':
            return self._send(404, {'error': 'POST /ask {"q": ...}'})
        try:
            n = int(self.headers.get('Content-Length', 0))
            q = json.loads(self.rfile.read(n) or b'{}').get('q', '').strip()
        except Exception as e:
            return self._send(400, {'error': f'bad request: {e}'})
        if not q:
            return self._send(400, {'error': 'missing "q"'})
        try:
            r = ask.respond(q)
            cites = sorted(set(re.findall(r'\[([^\]]+?:\d+(?:-\d+)?)\]', r['final'])))
            self._send(200, {'q': q, 'mode': r['mode'], 'answer': r['final'],
                             'struck': r['struck'], 'citations': cites, 'routed': r['routed']})
        except Exception as e:
            self._send(500, {'error': str(e)})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print(f"KB daemon starting on http://127.0.0.1:{PORT} "
          f"(rerank={os.environ.get('KB_RERANK')}, semantic_audit={os.environ.get('KB_SEMANTIC_AUDIT')}, "
          f"judge={os.environ.get('KB_SEMANTIC_JUDGE', 'llm')}, gen={ask.GEN_MODEL})", flush=True)
    _prewarm()
    print("  ready.", flush=True)
    HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
