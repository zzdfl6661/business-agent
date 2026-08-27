"""将店长通讯录一次性直接导入数据库，不提供 HTTP 上传接口。

用法：
    python -m scripts.import_store_contacts "D:\\path\\店长信息库.csv"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.mysql import get_session_factory, init_db  # noqa: E402
from services.contacts import import_contacts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性导入门店店长通讯录到 store_contacts")
    parser.add_argument("file", type=Path, help="CSV 或 XLSX 通讯录路径")
    args = parser.parse_args()
    source = args.file.expanduser().resolve()
    if not source.is_file():
        parser.error(f"文件不存在：{source}")

    init_db()
    with get_session_factory()() as session:
        result = import_contacts(source.name, source.read_bytes(), session)
    # 不打印姓名和手机号，避免终端历史留下个人信息。
    print(json.dumps({
        "success": result["success"],
        "file": result["file"],
        "total": result["total"],
        "imported": result["imported"],
        "invalid": result["invalid"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
