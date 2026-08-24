EduGate 单机课堂版（macOS Apple Silicon）

【系统要求】
macOS 11 或更高版本；Apple 芯片（M1、M2、M3、M4 或后续 arm64 机型）。本版本不支持 Intel Mac。

【安装与首次打开】
1. 解压完整 ZIP，把 EduGate.app 拖到“应用程序”文件夹，也可以留在当前文件夹使用。
2. 本包采用 ad-hoc 签名，未使用 Apple Developer ID 公证。首次打开时，请按住 Control 点击 EduGate.app，选择“打开”，再在确认框中点击“打开”。
3. 如果系统仍阻止打开，请前往“系统设置 → 隐私与安全性”，在底部找到 EduGate 的提示并选择“仍要打开”。
4. EduGate 启动后会自动打开浏览器中的教师控制台。

【开始上课】
1. 首次使用时，在“系统 → 上游模型 API 管理”中添加并测试模型公司 API。
2. 在“控制”页点击“开始课堂”，再复制链接或二维码发给学生。
3. 初始账号为 admin，初始密码为 edugate；教师电脑默认自动进入。首次使用后建议修改密码。

【数据与迁移】
配置、模型密钥、知识库、课堂记录和日志保存在：
~/Library/Application Support/EduGate

迁移或备份前，请先在“系统”页点击“停止服务”。Mac 版数据不保存在 EduGate.app 内，升级应用时可以直接替换 EduGate.app，不会删除已有数据。模型密钥使用可迁移的便携格式保存，文件权限限制为当前用户读写；不要共享整个 EduGate 数据目录，也不要上传到公开网盘或代码仓库。

【网络】
默认使用 8000 端口；端口被占用时会自动尝试后续端口。学生设备必须和教师电脑位于可互访的同一局域网。若学生无法访问，请在“系统设置 → 网络 → 防火墙”中允许 EduGate 接收入站连接，并确认校园无线网络没有启用客户端隔离。

【退出】
“结束课堂”只停用学生入口，教师控制台仍会运行。需要完全退出时，请在教师端“系统”页点击“停止服务”。

English summary: this build supports Apple Silicon Macs on macOS 11 or later. Control-click EduGate.app and choose Open on first launch. Application data is stored in ~/Library/Application Support/EduGate and remains in place when the app is upgraded.
