import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_FILE = ROOT / "app_gradio.py"
APP_URL = "http://127.0.0.1:7860"


def wait_for_server(timeout_seconds: int = 60):
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            import urllib.request

            with urllib.request.urlopen(APP_URL, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main():
    print("正在启动库存分析应用...")
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    process = None
    if getattr(sys, "frozen", False):
        import app_gradio

        app_gradio.demo.launch(
            server_name="127.0.0.1",
            server_port=7860,
            prevent_thread_lock=True,
            show_error=True,
        )
    else:
        process = subprocess.Popen(
            [sys.executable, str(APP_FILE)],
            cwd=str(ROOT),
            env=env,
            stdout=None,
            stderr=None,
        )

    try:
        if wait_for_server():
            print(f"应用已启动，正在打开浏览器：{APP_URL}")
            webbrowser.open(APP_URL, new=2)
            print("按 Ctrl+C 可停止服务")
            if process is not None:
                process.wait()
            else:
                while True:
                    time.sleep(1)
        else:
            print("应用启动超时，请检查环境配置。")
            if process is not None:
                process.terminate()
            raise SystemExit(1)
    except KeyboardInterrupt:
        print("正在关闭应用...")
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            import app_gradio

            app_gradio.demo.close()


if __name__ == "__main__":
    main()
