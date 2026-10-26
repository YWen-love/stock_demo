"""魔搭（ModelScope）gradio 空间入口。

魔搭 gradio SDK 默认执行 `python app.py`，并通过环境变量注入监听地址：
  GRADIO_SERVER_NAME=0.0.0.0
  GRADIO_SERVER_PORT=7860

本地开发仍用 `python main.py`（支持 --host / --port / --reseed）。
两者共享同一份 advisor/ui 代码，只是入口参数不同。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advisor.config import LLM  # noqa: E402
from advisor.paths import db_path  # noqa: E402
from advisor.ui import launch  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger(__name__)
path = db_path()
logger.info("数据库：%s", path)
if LLM.enabled:
    logger.info("模型：%s @ %s", LLM.model, LLM.base_url)
else:
    logger.warning("未配置 MINIMAX_*，以纯规则模式运行（结论一样准，只是没那么会说话）")

# 魔搭注入 GRADIO_SERVER_NAME / GRADIO_SERVER_PORT；本地兜底 0.0.0.0:7860
server_name = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0")
server_port = int(os.getenv("GRADIO_SERVER_PORT", "7860"))

launch(db_path=path, server_name=server_name, server_port=server_port)
