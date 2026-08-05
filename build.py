#!/usr/bin/env python3
"""Build production assets.

  python build.py

  - assets/style.min.css  <- minified from src/style.css
  - assets/app.min.js     <- obfuscated + minified from src/app.js
                              (javascript-obfuscator via npx,
                               falls back to terser, then to plain copy)

Run this before deploying. The generated files are what users receive.
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "assets")


def ensure_out():
    os.makedirs(OUT, exist_ok=True)


def minify_css(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s*([{};:,>])\s*", r"\1", css)
    css = re.sub(r"\s{2,}", " ", css)
    return css.strip()


def build_css():
    src = os.path.join(SRC, "style.css")
    dst = os.path.join(OUT, "style.min.css")
    with open(src, "r", encoding="utf-8") as f:
        data = f.read()
    out = minify_css(data)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"[css ] {dst} ({len(out)} bytes)")


def run_tool(cmd, timeout=600):
    cmd = list(cmd)
    if os.name == "nt" and cmd[0] == "npx":
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if npx:
            cmd[0] = npx
        else:
            print("  ! npx not found")
            return False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            print(f"  ! {e}")
            return False
    else:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            print(f"  ! {e}")
            return False
    if r.returncode != 0:
        print(f"  ! exited {r.returncode}: {r.stderr[:400]}")
        return False
    return True


def build_js():
    src = os.path.join(SRC, "app.js")
    dst = os.path.join(OUT, "app.min.js")

    obf = [
        "npx", "--yes", "javascript-obfuscator", src,
        "--output", dst,
        "--compact", "true",
        "--rename-globals", "true",
        "--identifier-names-generator", "hexadecimal",
        "--string-array", "true",
        "--string-array-encoding", "base64",
        "--string-array-threshold", "1",
        "--string-array-rotate", "true",
        "--string-array-shuffle", "true",
        "--string-array-index-shift", "true",
        "--dead-code-injection", "false",
    ]
    if run_tool(obf):
        print(f"[js  ] {dst} (obfuscated)")
        return True

    print("[js  ] javascript-obfuscator unavailable -> trying terser")
    terser = ["npx", "--yes", "terser", src, "-c", "-m", "-o", dst]
    if run_tool(terser):
        print(f"[js  ] {dst} (terser)")
        return True

    print("[js  ] terser unavailable -> copying source (NOT protected)")
    shutil.copyfile(src, dst)
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ensure_out()
    build_css()
    ok = build_js()
    if not ok:
        print("[warn] JS fallback to plain copy - trang van chay nhung KHONG duoc bao ve.")
        sys.exit(1)
    print("[done] Assets san sang trong assets/")


if __name__ == "__main__":
    main()