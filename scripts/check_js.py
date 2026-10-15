#!/usr/bin/env python3
"""Syntax-check the inline <script> of the browser client with node (CI and local)."""
import re, subprocess, sys, tempfile
js = re.search(r"<script>(.*)</script>", open("live/static/index.html").read(), re.S).group(1)
f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
f.write(js); f.close()
sys.exit(subprocess.call(["node", "--check", f.name]))
