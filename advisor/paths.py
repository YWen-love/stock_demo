"""路径解析：开发态 / PyInstaller 打包态 / 自定义数据目录。

沿用 hr-ai-assessment/app/paths.py 的写法：打包后资源在 exe 同级目录或 _MEIPASS，
数据目录允许用环境变量覆盖（演示时不想污染项目目录就指到别处）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """项目根：打包后是 exe 所在目录，开发态是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """只读资源根：stock_rag / rag_docs 这类随包分发的文件。"""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root)
    return project_root()


def data_root() -> Path:
    """可写数据根：SQLite 落在这里。

    ADVISOR_DATA_DIR > APP_DATA_DIR > <项目根>/data
    """
    for env_key in ("ADVISOR_DATA_DIR", "APP_DATA_DIR"):
        configured = os.getenv(env_key)
        if configured:
            path = Path(configured)
            break
    else:
        path = project_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """数据库文件路径。可用 ADVISOR_DB_PATH 覆盖（测试用临时库）。"""
    configured = os.getenv("ADVISOR_DB_PATH")
    if configured:
        return Path(configured)
    return data_root() / "inventory.db"