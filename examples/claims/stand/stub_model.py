"""A stub of an OpenAI-compatible model for the stand job: no model account in CI.

The skills host of the example asks its model through ``SKILL_LLM_PROVIDER=openai``
(``POST <base>/chat/completions``). This stub answers the classification of
claims.classify@1 by keywords, deterministically, so the end-to-end run checks the
wiring — the skill, the executor, the core — and not a model. In a real installation
``SKILL_LLM_BASE_URL`` points at a real model.

    python stub_model.py --port 8081      # SKILL_LLM_BASE_URL=http://127.0.0.1:8081/v1
    python stub_model.py --host 0.0.0.0   # in a container of the stand network
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEYWORDS = (
    ("billing", ("charged", "invoice", "refund of the charge", "billing")),
    ("delivery", ("late", "delivery", "courier", "lost")),
    ("defect", ("broken", "stopped", "leak", "fault", "defect", "spark")),
)
MAX_BODY = 256 * 1024
SEVERE = ("spark", "smoke", "fire", "injur", "lawyer", "court")


def classify(text: str) -> dict[str, Any]:
    lowered = text.lower()
    category = next((c for c, words in KEYWORDS if any(w in lowered for w in words)), "other")
    severity = "high" if any(w in lowered for w in SEVERE) else "medium"
    return {"category": category, "severity": severity, "confidence": 0.9}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        print(f"stub-model: {self.command} {self.path}", flush=True)

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(HTTPStatus.NOT_FOUND, {"error": {"message": "no such route"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._send(HTTPStatus.BAD_REQUEST, {"error": {"message": "bad Content-Length"}})
            return
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(HTTPStatus.BAD_REQUEST, {"error": {"message": "a JSON body is expected"}})
            return
        asked = " ".join(
            str(m.get("content", ""))
            for m in request.get("messages", [])
            if m.get("role") == "user"
        )
        answer = json.dumps(classify(asked))
        self._send(
            HTTPStatus.OK,
            {
                "id": "stub",
                "object": "chat.completion",
                "model": request.get("model", "stub"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(asked) // 4,
                    "completion_tokens": 12,
                    "total_tokens": len(asked) // 4 + 12,
                },
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    print(f"stub model on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
