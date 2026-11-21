"""Loopback-only desktop service for Weapon Paint Studio."""
from pathlib import Path
import argparse
import base64
import gzip
import http.client
import http.server
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import studio_core as core
import weapon_baker as baker
import weapon_export
import export_jobs
import asset_management
import stock_assets

HOST = "127.0.0.1"
TOKEN = secrets.token_urlsafe(32)
PREVIEW_LOCK = threading.Lock()
mimetypes.add_type("text/javascript", ".js")


def local_request(url, path, post=False):
    parsed = urllib.parse.urlparse(url)
    connection = http.client.HTTPConnection(HOST, parsed.port, timeout=3)
    try:
        connection.request("POST" if post else "GET", path + "?" + parsed.query, body=b"{}" if post else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise ValueError("本地会话无效")
        return json.loads(raw)
    finally:
        connection.close()


def existing_session():
    try:
        session = json.loads((core.DATA / "tmp/session.json").read_text(encoding="utf-8"))
        if urllib.parse.urlparse(session["url"]).hostname != HOST:
            return None
        result = local_request(session["url"], "/api/health")
        return session if result.get("version") == core.VERSION and not result.get("closing") else None
    except (OSError, ValueError, KeyError):
        return None


class Desktop:
    def __init__(self, server, args, url):
        self.server, self.args, self.url = server, args, url
        self.process = None

    def start(self):
        host = core.INSTALL / "武器涂装工具.exe"
        command = [str(host)]
        if self.args.capture:
            command.append("--capture")
        if self.args.desktop_debug_port:
            command.append("--debug-port=" + str(self.args.desktop_debug_port))
        self.process = subprocess.Popen(command, env=dict(os.environ, HD2_WEAPON_DATA_DIR=str(core.DATA)),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self.watch, daemon=True).start()

    def watch(self):
        self.process.wait()
        self.server.closing = True
        self.server.shutdown()

    def focus(self):
        if self.process and self.process.poll() is None:
            subprocess.Popen([str(core.INSTALL / "武器涂装工具.exe")],
                             env=dict(os.environ, HD2_WEAPON_DATA_DIR=str(core.DATA)),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif not self.args.no_browser:
            self.start()
        return dict(opened=True)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def send(self, status, raw, content="application/json; charset=utf-8", cookie=False):
        compressed = len(raw) > 4096 and "gzip" in self.headers.get("Accept-Encoding", "") and (content.startswith("application/json") or content.startswith("text/"))
        if compressed:
            raw = gzip.compress(raw, compresslevel=3)
        self.send_response(status)
        for key, value in (("Content-Type", content), ("Content-Length", str(len(raw))), ("Cache-Control", "no-store"),
                           ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")):
            self.send_header(key, value)
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        if cookie:
            self.send_header("Set-Cookie", f"{self.server.cookie_name}={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        try:
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def auth(self):
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            return False
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        cookies = dict(pair.strip().split("=", 1) for pair in self.headers.get("Cookie", "").split(";") if "=" in pair)
        return (secrets.compare_digest(query.get("token", [""])[0], TOKEN) or
                secrets.compare_digest(cookies.get(self.server.cookie_name, ""), TOKEN))

    def do_GET(self):
        if not self.auth():
            self.send(403, b"Local session required", "text/plain")
            return
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if path == "/api/health":
                result = dict(version=core.VERSION, closing=self.server.closing, pid=os.getpid(), dataDirectory=str(core.DATA))
            elif path == "/api/stock-status":
                result = dict(ready=bool(core.PACK_HASH))
            elif path == "/api/catalog":
                pack_hash = urllib.parse.parse_qs(parsed.query).get("packHash", [core.PACK_HASH])[0]
                root = core.BUILTIN if pack_hash == core.PACK_HASH else core.inside(core.DATA / "assets/recovered", pack_hash)
                catalog = core.CATALOG if root == core.BUILTIN else core.read_catalog(root)
                result = dict(version=core.VERSION, categories=catalog["categories"], weapons=catalog["weapons"], packHash=pack_hash)
            elif path == "/api/recovery":
                result = core.restore()
            elif path == "/api/library":
                result = core.library()
            elif path == "/api/projects":
                result = [dict(path=target.relative_to(core.DATA).as_posix(), name=target.parent.parent.name + " / " + target.parent.name,
                               modified=target.stat().st_mtime) for target in (core.DATA / "projects").glob("*/*/project.hd2project")]
            elif path == "/api/export-status":
                result = export_jobs.snapshot(urllib.parse.parse_qs(parsed.query).get("jobId", [None])[0])
            elif path == "/api/asset-packs":
                result = asset_management.list_packs()
            elif path.startswith("/native-materials/"):
                parts = path.split("/")
                pack_hash = parts[2]
                if len(parts) != 4 or len(pack_hash) != 64 or any(character not in "0123456789abcdef" for character in pack_hash):
                    raise ValueError("资产锁无效")
                root = core.BUILTIN if pack_hash == core.PACK_HASH else core.DATA / "assets/recovered" / pack_hash
                target = core.inside(root / "preview", parts[3])
                self.send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")
                return
            elif path.startswith("/models/"):
                parts = path.split("/")
                pack_hash = parts[2]
                if len(pack_hash) != 64 or any(character not in "0123456789abcdef" for character in pack_hash):
                    raise ValueError("资产锁无效")
                root = core.BUILTIN if pack_hash == core.PACK_HASH else core.DATA / "assets/recovered" / pack_hash
                target = core.inside(root / "models", parts[3])
                self.send(200, target.read_bytes(), "application/octet-stream")
                return
            elif path.startswith("/images/"):
                target = core.image_path(path.split("/")[-1].split(".")[0])
                self.send(200, target.read_bytes(), "image/png")
                return
            elif path.startswith("/cache/") or path.startswith("/download/"):
                relative = path[1:] if path.startswith("/cache/") else path[len("/download/"):]
                if path.startswith("/download/") and not relative.startswith(("mods/", "projects/", "backups/")):
                    raise ValueError("文件下载范围无效")
                target = core.inside(core.INSTALL, relative) if relative.startswith("mods/") else core.inside(core.DATA, relative)
                self.send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")
                return
            else:
                target = core.inside(core.APP / "web", "index.html" if path == "/" else path[1:])
                self.send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream", path == "/")
                return
            self.send(200, core.json_bytes(result))
        except (ValueError, OSError, KeyError) as error:
            self.send(400, core.json_bytes(dict(error=str(error))))

    def do_POST(self):
        if not self.auth() or self.headers.get("Origin") not in (None, f"http://{HOST}:{self.server.server_port}"):
            self.send(403, b"Forbidden", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 512 * 1024 * 1024:
                raise ValueError("请求内容过大或为空")
            data = json.loads(self.rfile.read(length))
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/assembly":
                result = core.assembly(data["project"], data.get("weaponId"))
            elif path == "/api/preview":
                with PREVIEW_LOCK:
                    result = baker.preview(data["project"])
            elif path == "/api/autosave":
                result = core.autosave(data["project"])
            elif path == "/api/save":
                result = core.save_project(data["project"])
            elif path == "/api/open":
                raw = base64.b64decode(data["data"].split(",")[-1], validate=True) if data.get("data") else core.inside(core.DATA, data["path"]).read_bytes()
                result = core.open_project(raw)
            elif path == "/api/new":
                result = core.new_project()
            elif path == "/api/import":
                result = core.import_image(data)
            elif path == "/api/library/remove":
                result = core.remove_library_image(data["hash"])
            elif path == "/api/text":
                result = core.text_image(data)
            elif path == "/api/export-plan":
                result = weapon_export.export_plan(data["project"], data.get("scope", "current"))
            elif path == "/api/export":
                result = export_jobs.start(data)
            elif path == "/api/export-cancel":
                result = export_jobs.cancel(data["jobId"])
            elif path == "/api/migrate":
                result = core.migration(data["project"], data["targetPack"], data.get("componentRemap", {}))
            elif path == "/api/assets/import":
                result = asset_management.import_pack(data)
            elif path == "/api/prepare-stock":
                result = stock_assets.prepare(data["gameDirectory"])
            elif path == "/api/focus":
                result = self.server.desktop.focus()
            elif path == "/api/quit":
                self.server.closing = True
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                result = dict(ok=True)
            else:
                raise ValueError("未知操作")
            self.send(200, core.json_bytes(result))
        except Exception as error:
            import traceback
            traceback.print_exc()
            self.send(400, core.json_bytes(dict(error=str(error))))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--desktop-debug-port", type=int, default=0)
    args = parser.parse_args()
    stock_assets.restore_existing_previews()
    asset_management.load_installed()
    (core.DATA / "tmp").mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(core.DATA / "tmp")
    os.environ["TMP"] = str(core.DATA / "tmp")
    existing = existing_session()
    if existing:
        if not args.no_browser:
            local_request(existing["url"], "/api/focus", True)
        return
    lock_stream = (core.DATA / "tmp/service.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        lock_stream.seek(0)
        lock_stream.write(b"0")
        lock_stream.flush()
        lock_stream.seek(0)
        try:
            msvcrt.locking(lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            for _ in range(30):
                time.sleep(.1)
                existing = existing_session()
                if existing:
                    if not args.no_browser:
                        local_request(existing["url"], "/api/focus", True)
                    return
            raise ValueError("工作台正在启动")
    server = http.server.ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    server.closing = False
    server.cookie_name = "weapon_session_" + str(server.server_port)
    url = f"http://{HOST}:{server.server_port}/?token={TOKEN}"
    server.desktop = Desktop(server, args, url)
    core.atomic_write(core.DATA / "tmp/session.json", core.json_bytes(dict(url=url, pid=os.getpid(), version=core.VERSION)))
    serving = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=.2))
    serving.start()
    try:
        if not args.no_browser:
            server.desktop.start()
        serving.join()
    finally:
        export_jobs.shutdown()
        server.server_close()
        lock_stream.close()


if __name__ == "__main__":
    main()
