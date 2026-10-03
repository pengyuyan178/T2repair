import os
from pathlib import Path
import sys
import unittest


if __name__ == "__main__":
    if sys.platform != "linux" or os.environ.get("PYTHONHASHSEED") != "42":
        raise SystemExit("Run the full suite on Linux with PYTHONHASHSEED=42.")
    os.environ.setdefault("CAUSALGUI_TOOL_PYTHON", sys.executable)
    os.environ.setdefault("CAUSALGUI_NODE_MODULES", str(Path(__file__).resolve().parents[1] / "node_modules"))
    if not os.environ.get("CAUSALGUI_CHROMIUM"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as browser:
            os.environ["CAUSALGUI_CHROMIUM"] = browser.chromium.executable_path
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
