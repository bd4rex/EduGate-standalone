EduGate 单机课堂便携版

【开始上课】
1. 双击 EduGate-Standalone.exe。
2. 浏览器会自动打开教师控制台并在本机自动进入。
3. 首次使用时，在“系统 -> 上游模型 API 管理”中添加并测试模型公司 API。
4. 在“控制”页最下方的学生课堂入口点击“开始课堂”，再复制链接发给学生。

【下课】
在学生课堂入口旁点击“结束课堂”会停用学生链接和学生会话，教师控制台仍保持运行。需要复制、移动或拔出本文件夹所在磁盘时，请再到“系统”页点击“停止服务”，等待网页停止响应。

【复制到另一台电脑】
必须复制整个 EduGate-Standalone 文件夹。配置、模型密钥、知识库和课堂记录保存在 EXE 旁的 config、data 文件夹中，复制后可继续使用。不要只复制 EXE，也不要删除 _internal（含浏览器 Python 离线资源）。新版本不再提供 runtime\python 学生解释器。

【学生 Python】
Python 在学生浏览器内执行，教师电脑不执行学生代码。Demo 提供运行、停止和输出；自制教学网页可用 EduGate.runPython()，外部网页可用 EduGateClient.runPython()。默认只提供标准库，无需公网下载；上报记录不是可信判分结果。旧 /run_python 接口已停用（鉴权后返回 410）。

【知识库文件夹】
“资源”页可为每个知识库打开教师电脑上的固定文件夹。把 TXT、MD、PDF 等资料放入后点击“扫描同步”，系统会添加新文件、更新变更文件并清理已删除文件的索引。默认 general 知识库不可删除。

【密码】
教师电脑默认自动进入控制台。初始账号 admin，初始密码 edugate，密码配置位于 config\edugate.env。学生电脑不能使用本机自动登录。

【网络】
首次运行若 Windows 防火墙询问，请允许“专用网络”，否则学生可能无法通过局域网连接。默认使用 8000 端口；端口被占用时 EduGate 会自动换用后续可用端口。

若多网卡或 VPN 导致链接地址不适用，在“系统 -> 高级设置 -> 学生入口 IP”选择实际局域网地址，保存并重启；此项不改变教师管理权限。

English summary: double-click EduGate-Standalone.exe, start and end student access in the browser teacher console, stop the service before moving the folder, and always copy the complete folder so config and data remain available.
