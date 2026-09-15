"""
safegate.execution.hil_emulator
===============================

An HTTP server that speaks `safegate-hil/1` and answers runs from the SIL
world. It exists to test the HilRunner contract (identity, digests, refusal
on a scanner configuration mismatch, a rig that changes mid-campaign)
without a physical rig.

It always reports `physical: false`. HilRunner therefore refuses it unless
explicitly allowed, and records its runs at SIL tier. Nothing produced by
this module can satisfy a HIL evidence requirement.

    python -m safegate.execution.hil_emulator --sut sut_config.yaml --port 8765
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.model import ConcreteRun, ExecutionTier, Pinning
from ..sim.core import SutConfig
from ..sim.runner import SilRunner, simulator_digest


class EmulatedRig:
    def __init__(self, sut: SutConfig, rig_id: str = "emulated-rig-0") -> None:
        self.runner = SilRunner(sut)
        self.rig_id = rig_id
        self.firmware_digest = "emulated-" + sut.digest()[:24]
        self.rig_image_digest = simulator_digest(self.runner.dt)
        self.scanner_config_checksum = sut.digest()[:16]

    def identity(self) -> dict:
        return {
            "protocol": "safegate-hil/1",
            "rig_id": self.rig_id,
            "physical": False,
            "firmware_digest": self.firmware_digest,
            "rig_image_digest": self.rig_image_digest,
            "scanner_config_checksum": self.scanner_config_checksum,
        }

    def run(self, body: dict) -> tuple[int, dict]:
        expected = body.get("expected_scanner_config_checksum")
        if expected and expected != self.scanner_config_checksum:
            return 409, {
                "ok": False,
                "message": "scanner configuration checksum mismatch; rig refuses to run",
                **self._digests(),
            }
        run = ConcreteRun(
            test_case_ref="hil",
            scenario=body["scenario"],
            assignment={k: float(v) for k, v in body["assignment"].items()},
            tier=ExecutionTier.SIL,
            pinning=Pinning(**body["pinning"]),
        )
        out = self.runner.execute(run, Path("."))
        if not out.ok or out.trace is None:
            return 200, {"ok": False, "message": out.message, **self._digests()}
        return 200, {
            "ok": True,
            "message": "",
            "time": out.trace.time.tolist(),
            "signals": {
                k: v.tolist() for k, v in out.trace.signals.items() if not k.startswith("__")
            },
            **self._digests(),
        }

    def _digests(self) -> dict:
        return {
            "firmware_digest": self.firmware_digest,
            "rig_image_digest": self.rig_image_digest,
            "scanner_config_checksum": self.scanner_config_checksum,
        }


def make_server(rig: EmulatedRig, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/identity":
                self._send(200, rig.identity())
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self) -> None:
            if self.path != "/runs":
                self._send(404, {"message": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send(*rig.run(body))

        def log_message(self, *args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(rig: EmulatedRig) -> tuple[ThreadingHTTPServer, str]:
    server = make_server(rig)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="safegate-hil-emulator")
    ap.add_argument("--sut", required=True, type=Path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    rig = EmulatedRig(SutConfig.from_yaml(args.sut))
    server = make_server(rig, args.host, args.port)
    print(f"emulated rig {rig.rig_id} (physical: false) on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
