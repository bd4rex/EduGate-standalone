from __future__ import annotations


OPENAPI_TAGS = [
    {"name": "System", "description": "运行状态与本机管理。"},
    {"name": "Auth", "description": "教师登录与本机会话。"},
    {"name": "Classroom", "description": "课堂入口、学生会话与课堂记录。"},
    {"name": "Publishing", "description": "教学网页上传、发布与学生访问。"},
    {"name": "Chat", "description": "学生对话与流式输出。"},
    {"name": "OpenAI Compatible", "description": "受平台密钥保护的兼容接口。"},
    {"name": "Config", "description": "当前课堂策略。"},
    {"name": "Models", "description": "上游供应商与模型。"},
    {"name": "Knowledge", "description": "本地知识库。"},
    {"name": "Python", "description": "课堂短代码执行。"},
]

API_DOCS = {
    ("GET", "/health"): ("健康检查", "确认 EduGate 服务可用。"),
    ("POST", "/auth/local-session"): ("本机教师会话", "仅回环地址可换取便携版教师会话。"),
    ("POST", "/auth/setup"): ("初始化教师管理员", "仅允许从教师电脑首次设置。"),
    ("POST", "/auth/login"): ("教师登录", "使用单机管理员账号登录。"),
    ("POST", "/auth/logout"): ("退出登录", "撤销当前教师会话。"),
    ("POST", "/auth/password"): ("修改密码", "修改单机管理员密码并撤销旧会话。"),
    ("POST", "/classroom/join"): ("加入课堂", "用课堂令牌换取独立学生会话。"),
    ("POST", "/chat"): ("课堂对话", "使用当前课堂策略请求上游模型。"),
    ("POST", "/chat/stream"): ("流式课堂对话", "以 SSE 返回上游模型输出。"),
    ("POST", "/v1/chat/completions"): ("OpenAI 兼容对话", "使用平台密钥访问当前课堂策略。"),
    ("GET", "/v1/models"): ("OpenAI 兼容模型列表", "返回稳定的 EduGate 虚拟模型标识。"),
    ("GET", "/config"): ("读取课堂配置", "读取当前模型、知识库和课堂策略。"),
    ("POST", "/config/model"): ("切换课堂模型", "切换单机课堂默认模型。"),
    ("POST", "/config/ai"): ("启停课堂 AI", "控制学生是否可以请求 AI。"),
    ("PUT", "/config/scenarios/{scenario_id}"): ("更新课堂策略", "更新指定课堂场景。"),
    ("GET", "/admin/classroom"): ("课堂入口状态", "读取课堂令牌和课堂周期。"),
    ("POST", "/admin/classroom/start"): ("开始课堂", "创建新的课堂令牌。"),
    ("POST", "/admin/classroom/rotate"): ("更换课堂链接", "撤销旧学生会话并创建新令牌。"),
    ("POST", "/admin/classroom/end"): ("结束课堂", "撤销课堂入口和全部学生会话。"),
    ("GET", "/admin/published-pages"): ("教学网页列表", "读取本机已上传网页和当前活动网页。"),
    ("POST", "/admin/published-pages"): ("上传教学网页", "上传单个 HTML 或包含 index.html 的 ZIP 并可立即发布。"),
    ("POST", "/admin/published-pages/{page_id}/activate"): ("发布教学网页", "把指定网页设置为学生课堂入口。"),
    ("POST", "/admin/published-pages/deactivate"): ("切换 Demo 测试页", "停止发布自定义网页，让学生入口使用内置 Demo。"),
    ("DELETE", "/admin/published-pages/{page_id}"): ("删除教学网页", "删除指定网页及其本地资源。"),
    ("GET", "/published-pages/{page_id}"): ("读取活动教学网页", "课堂开放时凭课堂令牌读取活动网页文档。"),
    ("GET", "/published-pages/{page_id}/assets/{asset_path}"): ("读取教学网页资源", "读取已上传网页的受限静态资源。"),
    ("POST", "/admin/system/platform-key/generate"): ("生成下游平台密钥", "生成并仅回显一次新的下游 API 密钥。"),
    ("DELETE", "/admin/system/platform-key"): ("关闭下游平台接口", "删除下游平台密钥并关闭 OpenAI 兼容接口。"),
    ("GET", "/admin/classroom-records"): ("课堂记录", "读取本机管理员名下的课堂记录。"),
    ("GET", "/admin/classroom-records/{run_id}"): ("课堂详情", "读取一节课堂的互动记录。"),
    ("DELETE", "/admin/classroom-records/{run_id}"): ("删除课堂记录", "永久删除一节课堂的本地记录。"),
    ("GET", "/admin/models"): ("模型目录", "读取已导入的模型。"),
    ("POST", "/admin/models/discover"): ("发现上游模型", "读取一个 OpenAI-compatible 供应商的模型列表。"),
    ("POST", "/admin/models/batch-import"): ("批量导入模型", "把选中的上游模型加入本机目录。"),
    ("DELETE", "/admin/models/{model_id}"): ("删除模型", "删除模型并按需切换课堂引用。"),
    ("GET", "/knowledge/sources"): ("知识库列表", "读取本机全部知识库。"),
    ("POST", "/knowledge/sources"): ("保存知识库", "新建或更新知识库。"),
    ("POST", "/knowledge/sources/{source_id}/scan"): ("扫描知识库", "同步固定目录中的支持文件。"),
    ("POST", "/run_python"): ("运行 Python", "运行受限的课堂短代码。"),
    ("POST", "/run_python/stream"): ("流式运行 Python", "以 SSE 返回排队、输出和完成状态。"),
}

def tag_for_path(path: str) -> str:
    if path == "/health" or path.startswith("/admin/system") or path.startswith("/admin/dashboard"):
        return "System"
    if path.startswith("/auth/"):
        return "Auth"
    if path.startswith("/classroom/") or path.startswith("/admin/classroom"):
        return "Classroom"
    if path.startswith("/published-pages/") or path.startswith("/admin/published-pages"):
        return "Publishing"
    if path in {"/chat", "/chat/stream"}:
        return "Chat"
    if path.startswith("/v1/"):
        return "OpenAI Compatible"
    if path.startswith("/config"):
        return "Config"
    if path.startswith("/admin/models") or path.startswith("/admin/providers"):
        return "Models"
    if path.startswith("/knowledge/"):
        return "Knowledge"
    if path.startswith("/run_python"):
        return "Python"
    return "System"
