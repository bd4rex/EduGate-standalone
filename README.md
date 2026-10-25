# EduGate v2.4.0 运行源码归档

**这是 2026-10-25 补录的过程资料。** 原始 Windows ZIP 保持原样；本 Git 提交没有伪装成当时已有的提交，也没有回填历史提交日期。

## 本版变化

完整 AI 试问：复用学生回答流程，检查问题、资料依据、最终回答及引用，支持追问，并独立于学生课堂记录。

## 来源与完整性

主程序源码取自正式 v2.4.0 提交，前端与附属文件取自正式发行 ZIP；全部经过同一套核对。

- 原构建批次：`2.4.0-20261025-094148`。
- 原始 ZIP：`EduGate-Standalone-2.4.0-windows-x64.zip`，261195135 字节。
- SHA-256：`03b21c9c6ab0b5c07faa88bb3a3882a0497be7be06ff009f651c15ad4db2d928`。
- 35 个后端模块和 1 个启动器的指令、常量、名称、参数及异常表一致；文件路径和行号调试信息不参与比对。
- 67 个发行文件逐字节保留；详细文件哈希与模块指纹见 [ARCHIVE.json](ARCHIVE.json)。

这不是完整的历史开发工程：无法核验的历史测试、构建脚本和源码注释没有冒充原件加入。本快照适合查看运行代码和版本差异；完整开发工程、测试及构建说明见[正式 v2.4.0 源码](https://github.com/bd4rex/EduGate-standalone/tree/v2.4.0)。

## 复核

使用 Python 3.12，并安装 PyInstaller。复核器只读取发行 ZIP、编译源码并比较代码对象，不执行归档程序。

```text
python tools/verify_release_snapshot.py --archive /path/to/EduGate-Standalone-2.4.0-windows-x64.zip --source .
```

普通使用请下载[最新正式版本](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.4.0)。历史包作为过程资料保留，不会替换 v2.4.0 的 Latest 标记。

此归档不含教师配置、密钥、课堂数据或本机升级备份。
