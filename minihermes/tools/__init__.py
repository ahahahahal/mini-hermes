"""
工具集合。

这里的每个 .py 文件都是"自注册"的：模块顶层调用 registry.register(...)，
被 registry.discover_builtin_tools() 通过 AST 扫描发现。

新增一个工具 = 新增一个文件。不需要动任何清单、不需要改 __init__.py。
"""
