#!/usr/bin/env python3
"""Serve the Starlink WebUI and expose a restricted server-directory API."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MAX_FILES = 20_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class WebUIRequestHandler(SimpleHTTPRequestHandler):
    web_root: Path
    data_root: Path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.web_root), **kwargs)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _resolve_data_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser() if raw_path else self.data_root
        if not candidate.is_absolute():
            candidate = self.data_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.data_root):
            raise PermissionError("所选目录不在允许的数据根目录内")
        if not candidate.is_dir():
            raise FileNotFoundError("目录不存在或不是文件夹")
        return candidate

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
        parsed = urlparse(self.path)
        if parsed.path != "/api/directories":
            return super().do_GET()

        try:
            raw_path = parse_qs(parsed.query).get("path", [""])[0]
            current = self._resolve_data_path(raw_path)
            directories = sorted(
                (
                    {"name": entry.name, "path": str(entry.resolve())}
                    for entry in current.iterdir()
                    if entry.is_dir() and not entry.name.startswith(".")
                ),
                key=lambda item: item["name"].casefold(),
            )
            self._send_json(
                {
                    "root": str(self.data_root),
                    "current": str(current),
                    "parent": str(current.parent) if current != self.data_root else None,
                    "directories": directories,
                }
            )
        except PermissionError as error:
            self._send_json({"error": str(error)}, 403)
        except (FileNotFoundError, OSError) as error:
            self._send_json({"error": str(error)}, 404)

    def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
        if urlparse(self.path).path != "/api/route-files":
            self._send_json({"error": "接口不存在"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            selected = self._resolve_data_path(str(request.get("path", "")))
            paths = sorted(
                (
                    path
                    for path in selected.rglob("*.csv")
                    if path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(self.data_root)
                ),
                key=lambda path: str(path.relative_to(selected)).casefold(),
            )
            if len(paths) > MAX_FILES:
                raise ValueError(f"CSV 文件超过上限 {MAX_FILES} 个")
            total_bytes = sum(path.stat().st_size for path in paths)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("CSV 总大小超过 512 MiB，请选择更小的目录")

            files = []
            for path in paths:
                files.append(
                    {
                        "name": path.name,
                        # Match webkitdirectory: its relative path includes the selected
                        # directory name, which is used by the UI to infer method names.
                        "relativePath": (Path(selected.name) / path.relative_to(selected)).as_posix(),
                        "text": path.read_text(encoding="utf-8-sig"),
                    }
                )
            self._send_json({"directory": str(selected), "files": files})
        except PermissionError as error:
            self._send_json({"error": str(error)}, 403)
        except (FileNotFoundError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, 400)

    def guess_type(self, path: str) -> str:
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Starlink WebUI 服务器目录版")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="允许网页浏览的数据根目录（默认项目根目录）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    web_root = Path(__file__).resolve().parent
    data_root = args.data_root.expanduser().resolve()
    if not data_root.is_dir():
        raise SystemExit(f"数据根目录不存在：{data_root}")

    WebUIRequestHandler.web_root = web_root
    WebUIRequestHandler.data_root = data_root
    server = ThreadingHTTPServer((args.host, args.port), WebUIRequestHandler)
    print(f"WebUI: http://{args.host}:{args.port}/starLinkMultiFlowWebUI.html")
    print(f"允许浏览的数据根目录: {data_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
