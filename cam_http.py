#!/usr/bin/env python3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8000

WIDTH = 640
HEIGHT = 480
FRAMERATE = 15
QUALITY = 80

latest_frame = None
frame_id = 0
frame_cond = threading.Condition()


def camera_worker():
    global latest_frame, frame_id

    cmd = [
        "rpicam-vid",
        "-t", "0",
        "-n",
        "--codec", "mjpeg",
        "--width", str(WIDTH),
        "--height", str(HEIGHT),
        "--framerate", str(FRAMERATE),
        "--quality", str(QUALITY),
        "-o", "-",
    ]

    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            buffer = bytearray()

            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    err = proc.stderr.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"camera process stopped: {err.strip()}")

                buffer.extend(chunk)

                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 1024 * 1024:
                            buffer.clear()
                        break

                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break

                    jpg = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]

                    with frame_cond:
                        latest_frame = jpg
                        frame_id += 1
                        frame_cond.notify_all()

        except Exception as e:
            print(f"[camera] {e}", flush=True)
            time.sleep(1)
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Pi Camera Stream</title>
  <style>
    body {{
      font-family: sans-serif;
      margin: 24px;
      background: #111;
      color: #eee;
    }}
    img {{
      max-width: 100%;
      height: auto;
      border: 1px solid #444;
    }}
    a {{
      color: #9cf;
    }}
    code {{
      background: #222;
      padding: 2px 6px;
      border-radius: 4px;
    }}
  </style>
</head>
<body>
  <h1>Pi Camera Stream</h1>
  <p>Live stream: <a href="/stream.mjpg"><code>/stream.mjpg</code></a></p>
  <p>Snapshot: <a href="/snapshot.jpg"><code>/snapshot.jpg</code></a></p>
  <img src="/stream.mjpg" alt="camera stream">
</body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/snapshot.jpg":
            with frame_cond:
                ok = frame_cond.wait_for(lambda: latest_frame is not None, timeout=10)
                frame = latest_frame if ok else None

            if frame is None:
                self.send_error(503, "No frame available yet")
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(frame)
            return

        if self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            last_seen = -1
            try:
                while True:
                    with frame_cond:
                        ok = frame_cond.wait_for(
                            lambda: latest_frame is not None and frame_id != last_seen,
                            timeout=15,
                        )
                        if not ok:
                            continue
                        frame = latest_frame
                        last_seen = frame_id

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    t = threading.Thread(target=camera_worker, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"HTTP stream running on http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
