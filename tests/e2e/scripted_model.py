"""A deterministic, OpenAI-compatible chat-completions server that stands in for the LLM.

Only the *model* is scripted; Hermes itself (CLI, plugin loader, slash commands, agent loop,
tool dispatch, subagent lifecycle, file tools, hooks) runs unmodified against it. Behaviour:

* main agent, continuation prompt "Graph Engineering autopilot: run X": call ge_graph
  action=auto run_id=X; call it again while the tool result says "continue": true; then answer
  with the tool's summary;
* node worker ("You are executing one node ..."): for 'Create `f` containing "t"' call the real
  write_file tool once, then answer with the declared outputs as a JSON block;
* anything else (titles, auxiliary calls): a short plain answer.

Every request is appended to a JSONL log so tests can prove what each agent context contained.
"""
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = None
_LOCK = threading.Lock()
SLOW_SECONDS = 90.0
_SLOWED: set = set()  # nodes whose "(slow)" worker turn was already delayed once (for kill tests)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _tool_names(body):
    return [t.get("function", {}).get("name") for t in body.get("tools") or []]


def _call(name, arguments):
    return {"id": "call_%d" % int(time.time() * 1e6), "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def decide(body):
    messages = body.get("messages") or []
    system = "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    users = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    tools = [m for m in messages if m.get("role") == "tool"]
    everything = system + "\n" + "\n".join(users)
    if "You are executing one node from an approved Graph Engineering run" in everything:
        node = re.search(r"Node: (\S+)", everything).group(1)
        workspace = re.search(r"Workspace directory: (.+?) \(keep", everything)
        purpose = everything.split("Purpose:\n", 1)[1].split("\n", 1)[0] if "Purpose:\n" in everything else ""
        if "(slow)" in purpose and node not in _SLOWED:
            _SLOWED.add(node)
            time.sleep(SLOW_SECONDS)  # the host process is killed while its worker waits for this reply
        match = re.search(r"[Cc]reate `([^`]+)` containing \"([^\"]+)\"", purpose)
        if match and workspace and not tools and "write_file" in _tool_names(body):
            path = workspace.group(1).rstrip("/") + "/" + match.group(1)
            return {"tool_calls": [_call("write_file", {"path": path, "content": match.group(2) + "\n"})]}
        contract = everything.split("Required output contract - a JSON object with exactly these keys:\n", 1)
        outputs = {}
        if len(contract) == 2:
            for line in contract[1].split("\n\n")[0].splitlines():
                name, _, kind = line[2:].partition(": ")
                outputs[name] = True if kind.startswith("boolean") else [] if kind.startswith("array") else \
                    "node %s done by an isolated worker" % node
        return {"content": "Done.\n```json\n%s\n```" % json.dumps(outputs)}
    run = None
    since = 0
    for index, message in enumerate(messages):  # the latest continuation request of this conversation
        if message.get("role") == "user":
            found = re.search(r"Graph Engineering autopilot: run (\S+)", _text(message.get("content")))
            if found:
                run, since = found, index
    names = _tool_names(body)
    if run and ("ge_graph" in names or "tool_call" in names):
        run_id = run.group(1)
        tools = [m for m in messages[since:] if m.get("role") == "tool"]  # results of this turn only
        if tools:
            last = _tool_result(_text(tools[-1].get("content")))
            if not last.get("continue"):
                return {"content": "Autopilot finished for %s.\n%s" % (run_id, last.get("summary") or
                                                                        json.dumps(last)[:500])}
        arguments = {"action": "auto", "run_id": run_id}
        if "ge_graph" in names:
            return {"tool_calls": [_call("ge_graph", arguments)]}
        # plugin tools are deferred behind Hermes' tool-search bridge; a model calls them through tool_call
        return {"tool_calls": [_call("tool_call", {"calls": [{"name": "ge_graph", "arguments": arguments}]})]}
    return {"content": "ok"}


def _tool_result(text):
    """The ge_graph JSON inside a direct or bridged (tool_call) tool result."""
    for candidate in (text,):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
        found = _find(value)
        if found is not None:
            return found
    return {}


def _find(value):
    if isinstance(value, dict):
        if "ok" in value and ("autopilot" in value or "continue" in value):
            return value
        for item in value.values():
            found = _find(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find(item)
            if found is not None:
                return found
    elif isinstance(value, str) and value.strip().startswith("{"):
        try:
            return _find(json.loads(value))
        except ValueError:
            return None
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send({"object": "list", "data": [{"id": "scripted-model", "object": "model",
                                                           "context_length": 128000}]})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        reply = decide(body)
        message = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            message["tool_calls"] = reply["tool_calls"]
        if LOG:
            with _LOCK, open(LOG, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"request": body, "reply": message}) + "\n")
        payload = {"id": "chatcmpl-%d" % int(time.time() * 1e6), "object": "chat.completion",
                   "created": int(time.time()), "model": body.get("model", "scripted-model"),
                   "choices": [{"index": 0, "message": message,
                                "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        if body.get("stream"):
            return self._stream(payload, message)
        self._send(payload)

    def _stream(self, payload, message):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            delta["tool_calls"] = [dict(c, index=i) for i, c in enumerate(message["tool_calls"])]
        chunk = dict(payload, object="chat.completion.chunk",
                     choices=[{"index": 0, "delta": delta, "finish_reason": None}])
        end = dict(payload, object="chat.completion.chunk",
                   choices=[{"index": 0, "delta": {}, "finish_reason": payload["choices"][0]["finish_reason"]}])
        for part in (chunk, end):
            self.wfile.write(("data: %s\n\n" % json.dumps(part)).encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve(port=0, log=None):
    global LOG
    LOG = log
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    srv = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0, sys.argv[2] if len(sys.argv) > 2 else None)
    print(srv.server_address[1], flush=True)
    threading.Event().wait()
