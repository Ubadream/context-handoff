# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Opt-in: the hooks-only adapter inside a real, unmodified Codex, with a loopback model.

Set CW_CODEX_TEST_BINARY to a stock codex executable. Uses an isolated CODEX_HOME and working
directory; no account, credentials or external model API. Hooks are trusted only through the
hashes Codex itself reports (install_codex_hooks.py trust), not by computing them here.
"""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

REPO = Path(__file__).resolve().parents[2]
BINARY = os.environ.get("CW_CODEX_TEST_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set CW_CODEX_TEST_BINARY to run against a real Codex")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tool_call(n):
    return {"type": "function_call", "call_id": f"call-{n}", "name": "exec_command",
            "arguments": json.dumps({"cmd": "echo fixture"})}


def message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def test_reminders_compaction_note_and_originals_in_stock_codex(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(); work.mkdir()
    (work / "HANDOFF.md").write_text("Requests: finish the fixture. Done: step one. Next: verify.", encoding="utf-8")
    requests = []
    # tokens per reply: 5500 crosses the first threshold (5000), 7500 the last (6500) and the
    # host's own auto-compact limit (7000), so Codex compacts before sampling again.
    replies = [(tool_call(0), 5500), (tool_call(1), 7500), (message("FIXTURE_SUMMARY"), 100), (message("DONE"), 100)]

    class Model(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests) - 1
            if self.path != "/v1/responses" or index >= len(replies):
                self.send_error(500, "unexpected request")
                return
            item, used = replies[index]
            events = [{"type": "response.created", "response": {"id": f"r{index}"}},
                      {"type": "response.output_item.done", "item": item | {"id": f"m{index}"}},
                      {"type": "response.completed", "response": {"id": f"r{index}", "usage": {
                          "input_tokens": used, "output_tokens": 10, "total_tokens": used + 10}}}]
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (home / "config.toml").write_text(
        'model = "mock-model"\nmodel_provider = "fixture"\napproval_policy = "never"\n'
        # The only command is `echo fixture`; read-only would reject it and no PostToolUse would follow.
        'sandbox_mode = "danger-full-access"\nmodel_context_window = 10000\nmodel_auto_compact_token_limit = 7000\n'
        '[model_providers.fixture]\nname = "Fixture"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'wire_api = "responses"\nrequest_max_retries = 0\nstream_max_retries = 0\nsupports_websockets = false\n',
        encoding="utf-8")
    (home / "context-handoff").mkdir()
    (home / "context-handoff" / "hooks.json").write_text('{"thresholds": [5000, 6500]}', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "ANTHROPIC_"))}
    env["CODEX_HOME"] = str(home)
    installer = [sys.executable, str(REPO / "adapters/codex/install_codex_hooks.py")]
    try:
        for step in (["apply"], ["trust", "--codex", BINARY]):
            done = subprocess.run(installer + step, env=env, cwd=work, capture_output=True, text=True,
                                  timeout=120, creationflags=NO_WINDOW)
            assert done.returncode == 0, done.stdout + done.stderr
        doctor = subprocess.run(installer + ["doctor", "--codex", BINARY], env=env, cwd=work, capture_output=True,
                                text=True, timeout=120, creationflags=NO_WINDOW).stdout
        assert doctor.count("trust=trusted") == 4, doctor
        result = subprocess.run([BINARY, "exec", "--skip-git-repo-check", "--json", "-C", str(work),
                                 "OLD_REQUEST_FIXTURE"], env=env, cwd=work, capture_output=True,
                                timeout=180, creationflags=NO_WINDOW)
        (tmp_path / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=1), encoding="utf-8")
        (tmp_path / "stderr.log").write_bytes(result.stderr)
        assert result.returncode == 0, result.stderr.decode(errors="replace")[-3000:]
        assert len(requests) == 4, (len(requests), str(tmp_path))
        bodies = [json.dumps(r["input"], ensure_ascii=False) for r in requests]
        # The reminder reaches the model before the compaction. Hook events carry no token counts and
        # Codex writes them to the rollout asynchronously, so it lands one tool call after the crossing.
        reminder = "[context-handoff] Context is at 5510 tokens"
        assert reminder in bodies[2] and "At the next natural break" in bodies[2]
        assert reminder not in bodies[3]  # gone with the compacted history; the note below replaces it
        # After compaction: the note names the handoff, says the request is not new, gives an originals ID.
        assert "The conversation was just compacted" in bodies[3]
        assert json.dumps(str(work / "HANDOFF.md"))[1:-1] in bodies[3] and "it is not a new request" in bodies[3]
        manifests = list((home / "context-handoff" / "originals").glob("*.json"))
        assert len(manifests) == 1
        ident = manifests[0].stem
        assert ident in bodies[3]
        hook = REPO / "adapters/codex/hooks/cw_codex_hooks.py"
        listed = subprocess.run([sys.executable, "-X", "utf8", str(hook), "list", ident], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
        # §1 is session setup the host injected before the first user message.
        assert "§2" in listed and "OLD_REQUEST_FIXTURE" in listed.splitlines()[1]
        got = subprocess.run([sys.executable, "-X", "utf8", str(hook), "get", ident + "§2"], env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
        assert "verified" in got and "user: OLD_REQUEST_FIXTURE" in got and "tool call exec_command" in got
    finally:
        server.shutdown()
        server.server_close()
