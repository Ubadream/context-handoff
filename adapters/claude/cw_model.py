"""分段建議、交接檢查要問模型時的唯一入口（Jev 可替換，1009）。

照順序找，找到就用：
  1. CW_MODEL_CMD：任何「從 stdin 讀提示、把回答印到 stdout」的指令，例如 `claude -p --model haiku`。
  2. 本機的 wife_l2.py（老公這台的 Jev／AGY Flash；CW_L2 可改位置）。
  3. 都沒有 → ModelUnavailable；呼叫端退成只用程式規則（照樣分段，不附建議）。
"""
import importlib.util
import os
import shlex
import subprocess
import time
from pathlib import Path

L2 = Path(os.environ.get("CW_L2") or Path.home() / ".local" / "bin" / "wife_l2.py")
L2_MODEL = "gemini-3.8-flash-high"
TIMEOUT = 300


class ModelUnavailable(RuntimeError):
    pass


def name():
    """會用哪個模型（給紀錄看）；沒有就回 None。"""
    if os.environ.get("CW_MODEL_CMD"):
        return os.environ["CW_MODEL_CMD"]
    if L2.exists():
        return L2_MODEL
    return None


def ask(prompt):
    """回 (回答文字, 秒數)。沒有可用的模型丟 ModelUnavailable，模型本身出錯照原樣丟。"""
    cmd = os.environ.get("CW_MODEL_CMD")
    if cmd:
        start = time.time()
        argv = cmd if os.name == "nt" else shlex.split(cmd)  # Windows 交給 shell 找 .cmd（claude.cmd）
        r = subprocess.run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT, shell=os.name == "nt")
        if r.returncode:
            raise RuntimeError(f"CW_MODEL_CMD 結束碼 {r.returncode}：{(r.stderr or r.stdout)[-500:]}")
        return r.stdout, round(time.time() - start, 1)
    if L2.exists():
        spec = importlib.util.spec_from_file_location("wife_l2", L2)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text, _, secs = mod.agy(prompt, L2_MODEL, str(Path.home()))
        return text, secs
    raise ModelUnavailable("沒設 CW_MODEL_CMD，也沒有本機 wife_l2.py：只切段、不附建議")
