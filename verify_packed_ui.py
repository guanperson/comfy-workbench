"""Launch the packed UI against an explicitly isolated data home, then stop it."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "ComfyUI-Workbench" / "ComfyUI-Workbench.exe"
def main() -> int:
    if not EXE.is_file():
        raise RuntimeError(f"EXE 不存在：{EXE}")
    # The packed EXE may be denied access to D:\chatgpt even when source Python
    # can write there. A unique sibling directory is writable and is not the
    # user's real `data` directory; the non-empty settings wins path selection.
    with tempfile.TemporaryDirectory(prefix="ui-smoke-", dir=EXE.parent) as temporary:
        test_home = Path(temporary)
        (test_home / "settings.json").write_text(
            '{"ui_theme":"light","start_page":"preprocess"}', encoding="utf-8"
        )
        log = test_home / "workbench.log"
        env = os.environ.copy()
        env["COMFY_WORKBENCH_HOME"] = str(test_home)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["COMFY_WORKBENCH_NO_DIALOG"] = "1"
        proc = subprocess.Popen(
            [str(EXE)], cwd=str(EXE.parent), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"测试实例 PID={proc.pid}", flush=True)
        try:
            until = time.monotonic() + 60
            while time.monotonic() < until:
                content = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                if "进入事件循环" in content and f"数据目录: {test_home}" in content:
                    print("PACKED_UI_START=OK ISOLATED_DATA=OK", flush=True)
                    break
                if proc.poll() is not None:
                    print(f"EXE 提前退出：{proc.returncode}\n{content[-2500:]}", flush=True)
                    return 1
                time.sleep(0.5)
            else:
                print("等待启动超时\n" + (content[-2500:] if content else "没有日志"), flush=True)
                return 2
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        if "--wd14" in sys.argv:
            # Optional real GPU check. Supply a model through WD14_MODELS_DIR;
            # never copy a user's model or settings into the release package.
            from PIL import Image
            probe = test_home / "gpu-probe.png"
            Image.new("RGB", (512, 512), (90, 135, 175)).save(probe)
            result = subprocess.run(
                [str(EXE), "--check-wd14", str(probe)], cwd=str(EXE.parent), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=180,
            )
            content = log.read_text(encoding="utf-8", errors="replace")
            if (result.returncode != 0 or "available=True provider='GPU (CUDA)'" not in content
                    or "gpu-probe.png ->" not in content):
                print("PACKED_GPU=FAILED\n" + content[-2500:], flush=True)
                return 3
            print("PACKED_GPU=OK", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
