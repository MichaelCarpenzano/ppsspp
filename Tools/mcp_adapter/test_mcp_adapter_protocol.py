#!/usr/bin/env python3
"""Unit tests for PPSSPP MCP adapter protocol helpers.

These tests avoid a live PPSSPP backend and focus on deterministic validation,
resource metadata, and trace diff behavior.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppsspp_mcp_server as server


class FakeClient:
    ws_url = "ws://127.0.0.1:8765/debugger"

    def backend_status(self) -> dict:
        return {"connected": False, "ws_url": self.ws_url}


class AdapterProtocolTests(unittest.TestCase):
    def test_vnext_tools_are_listed(self) -> None:
        names = {tool["name"] for tool in server.tool_definitions()}
        for name in {
            "backend_status",
            "record_inputs_start",
            "replay_run",
            "trace_start",
            "trace_export_manifest",
            "frame_hash",
            "memory_region_hash",
            "state_fingerprint",
            "diff_trace",
            "find_first_divergence",
        }:
            self.assertIn(name, names)

    def test_resources_are_listed(self) -> None:
        uris = {resource["uri"] for resource in server.resource_definitions()}
        self.assertEqual(
            {
                "ppsspp://capabilities",
                "ppsspp://session",
                "ppsspp://game",
                "ppsspp://memory-map",
                "ppsspp://trace-schema",
            },
            uris,
        )

    def test_invalid_step_count_returns_structured_error(self) -> None:
        session = server.AdapterSession(FakeClient())  # type: ignore[arg-type]
        with self.assertRaises(server.ToolValidationError) as raised:
            server.call_tool(session, "step_frame", {"count": -1})
        payload = server.structured_error(raised.exception)
        self.assertEqual("invalid_argument", payload["code"])
        self.assertFalse(payload["retryable"])

    def test_input_event_validation_rejects_unknown_button(self) -> None:
        with self.assertRaises(server.ToolValidationError):
            server.validate_input_event(
                {"frame": 0, "button": "bad", "action": "press"},
                line_no=1,
            )

    def test_diff_trace_finds_first_divergence(self) -> None:
        server.DEFAULT_ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=server.DEFAULT_ARTIFACT_ROOT) as tmp:
            root = Path(tmp)
            ref_stream = root / "ref_state.jsonl"
            tgt_stream = root / "tgt_state.jsonl"
            ref_events = [
                {"frame": 0, "kind": "state", "value": 1},
                {"frame": 1, "kind": "state", "value": 2},
            ]
            tgt_events = [
                {"frame": 0, "kind": "state", "value": 1},
                {"frame": 1, "kind": "state", "value": 3},
            ]
            ref_stream.write_text(
                "".join(json.dumps(e, sort_keys=True) + "\n" for e in ref_events),
                encoding="utf-8",
            )
            tgt_stream.write_text(
                "".join(json.dumps(e, sort_keys=True) + "\n" for e in tgt_events),
                encoding="utf-8",
            )
            ref_manifest = root / "ref_manifest.json"
            tgt_manifest = root / "tgt_manifest.json"
            ref_manifest.write_text(
                json.dumps(
                    {
                        "schema": server.TRACE_SCHEMA_VERSION,
                        "files": {
                            "state": {
                                "path": str(ref_stream),
                                "sha256": server.sha256_bytes(ref_stream.read_bytes()),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            tgt_manifest.write_text(
                json.dumps(
                    {
                        "schema": server.TRACE_SCHEMA_VERSION,
                        "files": {
                            "state": {
                                "path": str(tgt_stream),
                                "sha256": server.sha256_bytes(tgt_stream.read_bytes()),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            diff = server.diff_trace_manifests(ref_manifest, tgt_manifest, "state")
            self.assertTrue(diff["diverged"])
            self.assertEqual(1, diff["event_index"])
            self.assertEqual("value", diff["field_diff"]["path"])


if __name__ == "__main__":
    unittest.main()
