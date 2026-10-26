"""pytest 全局配置：把项目根放进 sys.path。

advisor 是顶层包，advisor/rag.py 又要 import 顶层包 stock_rag。
两个都在项目根下，测试从任意目录跑都能 import 到。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))