"""MockAgent: simulates a grok agent speaking the stdio JSON-RPC protocol.

Used for integration testing of asdaaas without hitting the real API.
Can run as a standalone script (spawned by asdaaas via subprocess) or
used in-process for unit-level testing of collect_response/send/etc.

When run as a script:
    python3 mock_agent.py --responses responses.json

When used in-process:
    writer = MockAgentWriter(stream)
    writer.send_speech_response(request_id, "Hello world", total_tokens=50000)

The frame protocol matches grok's stdio JSON-RPC:
  - JSON-RPC response frames (with id + result)
  - session/update notifications (agent_message_chunk, tool_call, etc.)
  - _x.ai/session/prompt_complete notifications
  - Streaming _meta with totalTokens

Response format (for --responses file or in-process list):
    {
        "speech": "Hello world",           # agent_message_chunk text
        "thoughts": "Let me think...",     # agent_thought_chunk text (optional)
        "tool_calls": ["read_file"],       # tool_call titles (optional)
        "total_tokens": 50000,             # totalTokens in _meta
    }

If responses run out, MockAgent returns a default "noted" response.
"""

import json
import sys


def _chunkify(text, chunk_size=10):
    """Split text into small chunks to simulate streaming.
    
    Preserves whitespace so that "".join(chunks) == original text.
    This matches how the real grok binary streams: each chunk includes
    its leading space, and collect_response concatenates with "".join().
    """
    if not text:
        return [text]
    # Simple approach: split at chunk_size boundaries, preserving all chars
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i:i + chunk_size])
    return chunks


def notification(method, params=None):
    """Build a JSON-RPC notification (no id)."""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def response(request_id, result=None):
    """Build a JSON-RPC response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result or {}}


def build_prompt_response_frames(request_id, speech="", thoughts="",
                                  tool_calls=None, total_tokens=50000):
    """Build the full sequence of frames for a prompt response.
    
    Returns a list of frame dicts in the order they should be written.
    This is the core of the mock -- it produces the exact frame sequence
    that the real grok binary produces.
    """
    frames = []
    meta_params = {"_meta": {"totalTokens": total_tokens}}

    # Tool calls first
    for title in (tool_calls or []):
        frames.append(notification("session/update", {
            "update": {"sessionUpdate": "tool_call", "title": title},
            **meta_params,
        }))

    # Thought chunks
    if thoughts:
        for chunk in _chunkify(thoughts):
            frames.append(notification("session/update", {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"text": chunk},
                },
                **meta_params,
            }))

    # Speech chunks
    if speech:
        for chunk in _chunkify(speech):
            frames.append(notification("session/update", {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": chunk},
                },
                **meta_params,
            }))

    # prompt_complete notification
    frames.append(notification("_x.ai/session/prompt_complete", {}))

    # Final JSON-RPC response with _meta
    frames.append(response(request_id, {
        "_meta": {
            "totalTokens": total_tokens,
            "modelId": "mock-model",
            "stopReason": "end_turn",
        }
    }))

    return frames


class MockAgentWriter:
    """Writes mock agent frames to a stream (or list for testing)."""

    def __init__(self, output=None):
        """output: file-like with write() or None (collects to self.frames)."""
        self.output = output
        self.frames = []

    def write_frame(self, frame):
        """Write a single JSON-RPC frame."""
        self.frames.append(frame)
        if self.output:
            self.output.write((json.dumps(frame) + "\n").encode("utf-8")
                              if hasattr(self.output, 'buffer') or isinstance(self.output.write.__code__.co_varnames[1:2], bytes)
                              else json.dumps(frame) + "\n")
            if hasattr(self.output, 'flush'):
                self.output.flush()

    def write_frames(self, frames):
        """Write a list of frames in order."""
        for f in frames:
            self.write_frame(f)

    def send_init_response(self, request_id):
        """Respond to initialize request."""
        self.write_frame(response(request_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "mock-grok", "version": "0.0.1"},
            "capabilities": {},
        }))

    def send_session_response(self, request_id, session_id="mock-session-001"):
        """Respond to session/load or session/new."""
        self.write_frame(response(request_id, {"sessionId": session_id}))

    def send_prompt_response(self, request_id, speech="", thoughts="",
                              tool_calls=None, total_tokens=50000):
        """Send the full prompt response frame sequence."""
        frames = build_prompt_response_frames(
            request_id, speech, thoughts, tool_calls, total_tokens)
        self.write_frames(frames)


def run_stdio(responses, session_id="mock-session-001", initial_tokens=50000):
    """Run as a stdio mock agent. Reads JSON-RPC from stdin, writes to stdout.
    
    This is the entry point when mock_agent.py is run as a subprocess by asdaaas.
    """
    response_index = 0
    total_tokens = initial_tokens

    def get_next():
        nonlocal response_index, total_tokens
        if response_index < len(responses):
            r = responses[response_index]
            response_index += 1
            total_tokens = r.get("total_tokens", total_tokens)
            return r
        return {"speech": "noted", "total_tokens": total_tokens}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            frame = response(request_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mock-grok", "version": "0.0.1"},
                "capabilities": {},
            })
            sys.stdout.write(json.dumps(frame) + "\n")
            sys.stdout.flush()

        elif method == "notifications/initialized":
            pass

        elif method in ("session/load", "session/new"):
            frame = response(request_id, {"sessionId": session_id})
            sys.stdout.write(json.dumps(frame) + "\n")
            sys.stdout.flush()

        elif method == "session/prompt":
            r = get_next()
            frames = build_prompt_response_frames(
                request_id,
                speech=r.get("speech", ""),
                thoughts=r.get("thoughts", ""),
                tool_calls=r.get("tool_calls"),
                total_tokens=r.get("total_tokens", total_tokens),
            )
            for f in frames:
                sys.stdout.write(json.dumps(f) + "\n")
            sys.stdout.flush()

        elif request_id is not None:
            sys.stdout.write(json.dumps(response(request_id, {})) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mock grok agent for testing")
    parser.add_argument("--responses", help="JSON file with response list")
    parser.add_argument("--session-id", default="mock-session-001")
    parser.add_argument("--initial-tokens", type=int, default=50000)
    args = parser.parse_args()

    responses = []
    if args.responses:
        with open(args.responses) as f:
            responses = json.load(f)

    run_stdio(responses, args.session_id, args.initial_tokens)
