# -*- coding: utf-8 -*-
"""临时诊断：确认服务能否取到中文名预览图"""
import io
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PORT = 8770
names = ["娜娜莉1.0.pmx__19__mask.png", "小贞.pmx__8__mask.png"]

for n in names:
    url = "http://127.0.0.1:%d/previews/%s" % (PORT, urllib.parse.quote(n))
    try:
        r = urllib.request.urlopen(url, timeout=6)
        print("OK   %-32s HTTP %s  %d bytes" % (n, r.status, len(r.read())))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("FAIL %-32s HTTP %s  body=%r" % (n, e.code, body[:120]))
    except Exception as e:
        print("ERR  %-32s %s: %s" % (n, type(e).__name__, e))
