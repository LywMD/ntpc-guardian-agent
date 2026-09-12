"""診斷：資料庫內機構類型與資料來源分布，確認公校是否已載入。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guardian import config  # noqa: E402


OUT = Path(__file__).resolve().parents[1] / "data" / "diag_types.txt"
_buf: list[str] = []


def print(*args: object) -> None:  # noqa: A001 - 故意遮蔽，統一收集輸出
    _buf.append(" ".join(str(a) for a in args))


def main() -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    def cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    inst_cols = cols("institutions")
    print(f"DB = {config.DB_PATH}")
    print(f"institutions 欄位 = {sorted(inst_cols)}")

    print("\n--- 機構類型分布 ---")
    for r in conn.execute(
        "SELECT inst_type, COUNT(*) AS n FROM institutions GROUP BY 1 ORDER BY n DESC"
    ):
        print(f"  {r['inst_type']!r:24} {r['n']}")

    for candidate in ("data_source", "source", "origin"):
        if candidate in inst_cols:
            print(f"\n--- {candidate} 分布 ---")
            for r in conn.execute(
                f"SELECT COALESCE({candidate}, '(null)') AS s, COUNT(*) AS n "
                "FROM institutions GROUP BY 1 ORDER BY n DESC"
            ):
                print(f"  {r['s']!r:36} {r['n']}")
            break

    print("\n--- 有財務資料的機構（依類型） ---")
    for r in conn.execute(
        "SELECT i.inst_type, COUNT(DISTINCT f.inst_id) AS n, COUNT(*) AS rows "
        "FROM financials f JOIN institutions i ON i.inst_id = f.inst_id "
        "GROUP BY 1 ORDER BY n DESC"
    ):
        print(f"  {r['inst_type']!r:24} 機構 {r['n']}  筆數 {r['rows']}")

    print("\n--- 全部機構名稱 ---")
    for r in conn.execute(
        "SELECT inst_id, name, inst_type FROM institutions ORDER BY inst_type, name"
    ):
        print(f"  [{r['inst_type']}] {r['name']}")

    print("\n--- 名稱含『國小/國民小學/國中/高中/學校』者 ---")
    hits = list(
        conn.execute(
            "SELECT name, inst_type FROM institutions WHERE name LIKE '%國小%' "
            "OR name LIKE '%國民小學%' OR name LIKE '%國中%' OR name LIKE '%高中%' "
            "OR name LIKE '%學校%'"
        )
    )
    if hits:
        for r in hits:
            print(f"  [{r['inst_type']}] {r['name']}")
    else:
        print("  （無）→ 公校決算書尚未載入為機構")

    conn.close()


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(_buf), encoding="utf-8")
