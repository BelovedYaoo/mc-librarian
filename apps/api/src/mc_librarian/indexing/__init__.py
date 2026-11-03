"""Java 符号索引：tree-sitter 解析、`db/<version>.sqlite` 构建与只读查询。

分层：

- `layout`：数据根里的 `db/<version>.sqlite` 与 `src/<version>/` 布局。
- `text`：token 估算与行数（与 `read` 的行号语义同口径）。
- `java`：tree-sitter Java 解析，产出 `ParsedFile`。
- `build`：扫描源码树、解析、原子写索引库。
- `query`：只读打开索引库，符号查找 / 反查引用 / 文件骨架。
"""
