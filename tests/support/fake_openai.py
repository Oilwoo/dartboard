"""Fake OpenAI-compatible chat server for tests.

usage: python fake_openai.py PORT MODE(ok|429|500) NAME ARGS_JSON
In "ok" mode it answers every request with a tool call carrying ARGS_JSON (Pydantic AI's structured output).
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, MODE, NAME, ARGS = int(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if MODE == "429":
            self.send_response(429)
            self.send_header("Retry-After", "30")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"rate limited (fake)"}}')
            return
        if MODE == "500":
            self.send_response(500)
            self.end_headers()
            return
        args = dict(ARGS, summary=f"{ARGS.get('summary', '')} [{NAME}]".strip())
        tools = body.get("tools") or []
        msg = ({"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": tools[0]["function"]["name"], "arguments": json.dumps(args)}}]}
               if tools else {"role": "assistant", "content": json.dumps(args)})
        out = json.dumps({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                          "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tools else "stop"}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
