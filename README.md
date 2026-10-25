# EduGate v2.3.0 运行源码归档

**这是 2026-10-25 补录的过程资料。** 原始 Windows ZIP 保持原样；本 Git 提交没有伪装成当时已有的提交，也没有回填历史提交日期。

## 本版变化

便携目录隔离、同目录重复启动、安全保存退出，以及保留配置和课堂数据的程序升级。

## 来源与完整性

从原始发行 ZIP 逐字节提取前端、退出脚本、配置模板与许可证；从 v2.4.0 源码回退已知增量，并与原 EXE 的 35 个后端模块及启动器执行字节码逐项核对。

- 原构建批次：`2.3.0-20261024-105253`。
- 原始 ZIP：`EduGate-Standalone-2.3.0-windows-x64.zip`，261181017 字节。
- SHA-256：`4b75c6216f2dc58fbf5ebe08d6c5edea22cb2743e55c97a2f3593ca203ed1944`。
- 35 个后端模块和 1 个启动器的指令、常量、名称、参数及异常表一致；文件路径和行号调试信息不参与比对。
- 65 个发行文件逐字节保留；详细文件哈希与模块指纹见 [ARCHIVE.json](ARCHIVE.json)。

这不是完整的历史开发工程：无法核验的历史测试、构建脚本和源码注释没有冒充原件加入。本快照适合查看运行代码和版本差异；完整开发工程、测试及构建说明见[正式 v2.4.0 源码](https://github.com/bd4rex/EduGate-standalone/tree/v2.4.0)。

## 复核

使用 Python 3.12，并安装 PyInstaller。复核器只读取发行 ZIP、编译源码并比较代码对象，不执行归档程序。

```text
python tools/verify_release_snapshot.py --archive /path/to/EduGate-Standalone-2.3.0-windows-x64.zip --source .
```

普通使用请下载[最新正式版本](https://github.com/bd4rex/EduGate-standalone/releases/tag/v2.4.0)。历史包作为过程资料保留，不会替换 v2.4.0 的 Latest 标记。

此归档不含教师配置、密钥、课堂数据或本机升级备份。
