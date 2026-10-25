# 2.3 → 2.4 版本与代码过程归档

本页汇总本轮程序演进的发行物与可核验代码资料。历史归档补录于 **2026-10-25**，原始 ZIP 保持不变；日常安装使用 [Latest Release](https://github.com/bd4rex/EduGate-standalone/releases/latest)。

## 发行物

| 版本 | 阶段 | ZIP 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| [v2.3.0](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.3.0) | 便携目录、安全退出与保留数据升级 | 261181017 | `4b75c6216f2dc58fbf5ebe08d6c5edea22cb2743e55c97a2f3593ca203ed1944` |
| [v2.3.1](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.3.1) | 四页 UI 统一与二级选项卡 | 261188849 | `15dbf6a974c73dace7e44c509232d7b1cfd71445b7a7f8237bd8f1e685bc9d0e` |
| [v2.3.2](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.3.2) | 课堂范围、黑名单与统一检索试问 | 261193227 | `0c178386cf550034c346ed83098dcb37011fe1455551d21e3ab1503f36821873` |
| [v2.4.0](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.4.0) | 完整 AI 试问与公开发布 | 261195135 | `03b21c9c6ab0b5c07faa88bb3a3882a0497be7be06ff009f651c15ad4db2d928` |

各历史 Release 附原始 Windows ZIP、校验文件和源码来源说明。v2.4.0 是此前正式发布，历史版本不改变其发布记录。

## 源码来源和边界

当时 2.3.x 的完整源码没有逐版提交到 Git；main 在 `c83c5a6` 汇入了本轮累计修改，随后 `2f6ca54` 修正测试夹具。保留这些真实提交，不改写既有 main 历史。

为记录中间阶段，新建 [codex/archive-2.3-to-2.4 分支](https://github.com/bd4rex/EduGate-standalone/tree/codex/archive-2.3-to-2.4)，依次记录四版运行源码快照：

- 前端、退出辅助脚本、配置模板和许可证直接来自原始发行 ZIP，逐字节核对。
- 35 个后端应用模块及启动器，以正式 v2.4.0 源码为基础回退已知增量，再与各原 EXE 中的 Python 3.12 代码对象比较。四版全部核验通过。
- 比较包含指令、常量、名称、参数、标志和异常表；不比较文件路径、行号调试信息。这不证明原始注释、历史测试和构建脚本被完整保存，所以归档明确标为“核验还原”，不冒充完整历史开发工程。
- 归档提交使用本次整理日期，不回填原开发时间。原构建批次另见各快照的 ARCHIVE.json。
- 2.3.x 的 OpenAPI 版本字符串仍沿用 2.2.0；归档保持原件，产品版本以原 package.json 与页面版本为准。
- 本机教师配置、密钥、课堂数据、备份和真实模型验证内容均不在本次公开归档内。

## 可复核的代码演进

### 2.3.0 → 2.3.1

[查看逐文件代码差异](https://github.com/bd4rex/EduGate-standalone/compare/46c05e57d8118ad1c2b33ed971c0de758a6df62d...afc8b621e2e633241aea2dd69f024097a0b68a62)。

前端变化：`frontend/admin.html`、`frontend/assets/admin-settings.css`、`frontend/assets/admin-theme.css`、`frontend/assets/admin-workspaces.js`、`frontend/assets/knowledge-admin.js`。

后端模块：执行字节码一致。

### 2.3.1 → 2.3.2

[查看逐文件代码差异](https://github.com/bd4rex/EduGate-standalone/compare/afc8b621e2e633241aea2dd69f024097a0b68a62...5af6488b705b50cf41ca447194037b242dc00b06)。

前端变化：`frontend/admin.html`、`frontend/assets/admin-theme.css`、`frontend/assets/admin-workspaces.js`、`frontend/assets/knowledge-admin.js`。

后端模块：执行字节码一致。

### 2.3.2 → 2.4.0

[查看逐文件代码差异](https://github.com/bd4rex/EduGate-standalone/compare/5af6488b705b50cf41ca447194037b242dc00b06...ebb43cb0b3fa05c4b966e5811e87ded08976dc57)。

前端变化：`frontend/admin.html`、`frontend/assets/admin-theme.css`、`frontend/assets/knowledge-admin.js`。

后端模块：`app.api_docs`、`app.chat_service`、`app.main`、`app.routers.chat`。

## 复核方法

每个归档快照有 ARCHIVE.json、VERIFIED.json 和 tools/verify_release_snapshot.py。下载对应原始 Windows ZIP，使用 Python 3.12 和 PyInstaller 运行：

```text
python tools/verify_release_snapshot.py --archive /path/to/EduGate-Standalone-版本-windows-x64.zip --source .
```

复核器先验证 ZIP 摘要，只读取发行代码对象并编译源码比较，不执行归档程序。运行源码还原用于审阅和差异核对；当前完整开发工程、测试和打包步骤见正式分支与 [开发与测试](../开发与测试.md)。
