#!/usr/bin/env python3
"""Serve the kiosk's USB webcam over HTTP so several consumers can share it.

  /detect.mjpeg   multipart MJPEG stream (Frigate camera input)
  /snapshot.jpg   latest single frame (Nina "what do you see")

One ffmpeg reads /dev/video0 (JPEG passthrough, no decode); frames are split on JPEG markers,
thinned to WEBCAM_FPS and fanned out. Env: WEBCAM_DEV, WEBCAM_SIZE, WEBCAM_FPS, WEBCAM_PORT.
"""
import os, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEV = os.environ.get("WEBCAM_DEV", "/dev/video0")
SIZE = os.environ.get("WEBCAM_SIZE", "1280x720")
FPS = os.environ.get("WEBCAM_FPS", "5")
PORT = int(os.environ.get("WEBCAM_PORT", "8769"))

latest = {"jpg": None, "seq": 0, "t": 0.0}
cond = threading.Condition()


def reader():
    while True:
        cmd = ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg", "-video_size", SIZE,
               "-i", DEV, "-c:v", "copy", "-f", "image2pipe", "-"]   # camera JPEGs passed through, no re-encode
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        buf = b""
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    break
                frame, buf = buf[a:b + 2], buf[b + 2:]
                if time.time() - latest["t"] < 1.0 / float(FPS):
                    continue                      # drop frames down to FPS without decoding
                with cond:
                    latest.update(jpg=frame, seq=latest["seq"] + 1, t=time.time())
                    cond.notify_all()
        p.kill()
        time.sleep(3)  # camera busy/unplugged: retry


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/snapshot.jpg"):
            with cond:
                jpg = latest["jpg"]
            if not jpg or time.time() - latest["t"] > 10:
                self.send_error(503, "no recent frame")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
        elif self.path.startswith("/detect.mjpeg"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            seq = 0
            try:
                while True:
                    with cond:
                        cond.wait_for(lambda: latest["seq"] != seq, timeout=15)
                        seq, jpg = latest["seq"], latest["jpg"]
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)


threading.Thread(target=reader, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
