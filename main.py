"""对话式库存顾问 —— 启动入口。

    python main.py                 # 起对话界面
    python main.py --reseed        # 重建演示数据
    python main.py --port 7861     # 换端口
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from advisor.config import LLM  # noqa: E402
from advisor.paths import db_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="对话式库存顾问")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--reseed", action="store_true", help="重置数据库并写入演示数据")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s",
                        handlers=[logging.StreamHandler()])

    if args.reseed:
        from scripts.seed_demo import seed

        seed(db_path())

    logger = logging.getLogger(__name__)
    path = db_path()
    logger.info("数据库：%s", path)
    if LLM.enabled:
        logger.info("模型：%s @ %s", LLM.model, LLM.base_url)
    else:
        logger.warning("未配置 MINIMAX_*，以纯规则模式运行（结论一样准，只是没那么会说话）")

    from advisor.ui import launch

    launch(db_path=path, server_name=args.host, server_port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())