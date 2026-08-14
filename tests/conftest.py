# -*- coding: utf-8 -*-
"""pytest 公共配置：确保项目根目录可导入（无 backend/ 子目录）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # 让相对路径（.env / chroma_db / data/）与生产一致
