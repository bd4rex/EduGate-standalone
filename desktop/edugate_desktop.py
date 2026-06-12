import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class Session:
    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    username: str = ""
    role: str = ""
    display_name: str = ""


class EduGateClient:
    def __init__(self, session: Session) -> None:
        self.session = session

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        timeout: int = 60,
    ) -> Any:
        base_url = self.session.base_url.rstrip("/")
        data = None
        headers = {"Content-Type": "application/json"}
        if auth and self.session.token:
            headers["X-Admin-Token"] = self.session.token
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"连接失败：{error.reason}") from error
        return json.loads(raw) if raw else None


class EduGateDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EduGate 教师桌面端")
        self.geometry("1080x720")
        self.minsize(920, 620)

        self.session = Session()
        self.client = EduGateClient(self.session)
        self.config_data: dict[str, Any] = {}
        self.catalog: list[dict[str, Any]] = []
        self.teachers: list[dict[str, Any]] = []

        self._build_styles()
        self._build_ui()
        self._set_status("未登录")

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f5f7fb")
        style.configure("Panel.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("TLabel", background="#f5f7fb", foreground="#172033")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#172033")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Subtitle.TLabel", foreground="#607089")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", padding=(14, 8))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="EduGate 教师桌面端", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar()
        ttk.Label(header, textvariable=self.status_var, style="Subtitle.TLabel").grid(row=0, column=1, sticky="e")

        notebook = ttk.Notebook(root)
        notebook.grid(row=1, column=0, sticky="nsew")

        self.login_tab = ttk.Frame(notebook, padding=14)
        self.control_tab = ttk.Frame(notebook, padding=14)
        self.chat_tab = ttk.Frame(notebook, padding=14)

        notebook.add(self.login_tab, text="登录")
        notebook.add(self.control_tab, text="课堂控制")
        notebook.add(self.chat_tab, text="聊天测试")

        self._build_login_tab()
        self._build_control_tab()
        self._build_chat_tab()

    def _build_login_tab(self) -> None:
        self.login_tab.columnconfigure(0, weight=1)
        panel = ttk.Frame(self.login_tab, style="Panel.TFrame", padding=18)
        panel.grid(row=0, column=0, sticky="ew")
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="后端地址", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=6)
        self.base_url_var = tk.StringVar(value=DEFAULT_BASE_URL)
        ttk.Entry(panel, textvariable=self.base_url_var).grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(panel, text="用户名", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        self.username_var = tk.StringVar(value="admin")
        ttk.Entry(panel, textvariable=self.username_var).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(panel, text="密码", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        self.password_var = tk.StringVar()
        ttk.Entry(panel, textvariable=self.password_var, show="*").grid(row=2, column=1, sticky="ew", pady=6)

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="登录并加载配置", style="Primary.TButton", command=self.login).pack(side=tk.LEFT)
        ttk.Button(actions, text="刷新配置", command=self.load_all).pack(side=tk.LEFT, padx=8)

        self.identity_text = tk.Text(panel, height=8, wrap=tk.WORD, relief=tk.FLAT)
        self.identity_text.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.identity_text.insert(tk.END, "登录后这里会显示教师身份和可复制 API。")
        self.identity_text.configure(state=tk.DISABLED)

    def _build_control_tab(self) -> None:
        self.control_tab.columnconfigure(0, weight=1)
        self.control_tab.columnconfigure(1, weight=1)
        self.control_tab.rowconfigure(1, weight=1)

        left = ttk.Frame(self.control_tab, style="Panel.TFrame", padding=14)
        right = ttk.Frame(self.control_tab, style="Panel.TFrame", padding=14)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0))
        left.columnconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(5, weight=1)

        ttk.Label(left, text="模型与开关", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.ai_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="允许学生使用 AI", variable=self.ai_enabled_var, command=self.save_ai_enabled).grid(
            row=1, column=0, sticky="w", pady=(10, 6)
        )

        self.model_list = tk.Listbox(left, height=12, exportselection=False)
        self.model_list.grid(row=2, column=0, sticky="nsew", pady=8)
        self.model_list.bind("<<ListboxSelect>>", self.select_model)
        left.rowconfigure(2, weight=1)

        ttk.Button(left, text="重新加载模型", command=self.load_all).grid(row=3, column=0, sticky="ew", pady=(8, 0))

        ttk.Label(right, text="AI 策略", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(right, text="Temperature", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 2))
        self.temperature_var = tk.DoubleVar(value=0.4)
        ttk.Scale(right, from_=0, to=2, variable=self.temperature_var, orient=tk.HORIZONTAL).grid(
            row=2, column=0, sticky="ew"
        )

        self.strict_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="严格知识库模式", variable=self.strict_var).grid(row=3, column=0, sticky="w", pady=8)

        ttk.Label(right, text="System Prompt", style="Panel.TLabel").grid(row=4, column=0, sticky="w")
        self.prompt_text = tk.Text(right, height=14, wrap=tk.WORD)
        self.prompt_text.grid(row=5, column=0, sticky="nsew", pady=6)

        ttk.Label(right, text="知识库", style="Panel.TLabel").grid(row=6, column=0, sticky="w", pady=(10, 2))
        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(right, textvariable=self.source_var, state="readonly")
        self.source_combo.grid(row=7, column=0, sticky="ew")

        ttk.Button(right, text="保存策略", style="Primary.TButton", command=self.save_scenario).grid(
            row=8, column=0, sticky="ew", pady=(14, 0)
        )

    def _build_chat_tab(self) -> None:
        self.chat_tab.columnconfigure(0, weight=1)
        self.chat_tab.rowconfigure(1, weight=1)

        self.question_text = tk.Text(self.chat_tab, height=5, wrap=tk.WORD)
        self.question_text.grid(row=0, column=0, sticky="ew")
        self.question_text.insert(tk.END, "我想知道关于 IPv4 的知识，比如它的分类")

        self.answer_text = tk.Text(self.chat_tab, wrap=tk.WORD)
        self.answer_text.grid(row=1, column=0, sticky="nsew", pady=10)

        actions = ttk.Frame(self.chat_tab)
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="发送测试问题", style="Primary.TButton", command=self.send_chat).pack(side=tk.LEFT)
        ttk.Button(actions, text="复制教师 API 示例", command=self.copy_api_example).pack(side=tk.LEFT, padx=8)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _run_async(self, label: str, fn) -> None:
        self._set_status(label)

        def worker() -> None:
            try:
                result = fn()
                self.after(0, lambda: self._set_status("就绪"))
                return result
            except Exception as exc:
                self.after(0, lambda: self._show_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_error(self, exc: Exception) -> None:
        self._set_status("出错")
        messagebox.showerror("EduGate", str(exc))

    def login(self) -> None:
        def task() -> None:
            self.session.base_url = self.base_url_var.get().strip() or DEFAULT_BASE_URL
            data = self.client.request(
                "POST",
                "/auth/login",
                {
                    "username": self.username_var.get().strip(),
                    "password": self.password_var.get(),
                },
                auth=False,
            )
            teacher = data.get("teacher") or {}
            self.session.token = data.get("access_token", "")
            self.session.username = teacher.get("username", "")
            self.session.role = teacher.get("role", "")
            self.session.display_name = teacher.get("display_name", "")
            self.after(0, self._render_identity)
            self.after(0, self.load_all)

        self._run_async("登录中...", task)

    def load_all(self) -> None:
        if not self.session.token:
            messagebox.showwarning("EduGate", "请先登录。")
            return

        def task() -> None:
            self.config_data = self.client.request("GET", "/config")
            self.catalog = self.client.request("GET", "/model-catalog")
            self.teachers = self.client.request("GET", "/admin/teachers")
            self.after(0, self._render_config)
            self.after(0, self._render_identity)

        self._run_async("加载配置中...", task)

    def _render_identity(self) -> None:
        teacher_id = self.session.username
        api_example = self._api_example(teacher_id)
        text = (
            f"用户名：{self.session.username}\n"
            f"显示名称：{self.session.display_name}\n"
            f"角色：{self.session.role}\n\n"
            f"学生端 API 示例：\n{api_example}"
        )
        self.identity_text.configure(state=tk.NORMAL)
        self.identity_text.delete("1.0", tk.END)
        self.identity_text.insert(tk.END, text)
        self.identity_text.configure(state=tk.DISABLED)

    def _render_config(self) -> None:
        scenario = (self.config_data.get("scenarios") or {}).get("default") or {}
        self.ai_enabled_var.set(bool(scenario.get("ai_enabled", True)))
        self.temperature_var.set(float(scenario.get("temperature", 0.4)))
        self.strict_var.set(bool(scenario.get("knowledge_strict", False)))
        self.prompt_text.delete("1.0", tk.END)
        self.prompt_text.insert(tk.END, scenario.get("system_prompt", ""))

        sources = self.config_data.get("knowledge_sources") or []
        source_values = [f"{item.get('id')} | {item.get('name')}" for item in sources]
        self.source_combo.configure(values=source_values)
        active_source = scenario.get("knowledge_source_id") or ""
        for value in source_values:
            if value.startswith(f"{active_source} |"):
                self.source_var.set(value)
                break
        else:
            self.source_var.set(source_values[0] if source_values else "")

        active_model = scenario.get("model", "")
        self.model_list.delete(0, tk.END)
        for item in self.catalog:
            label = f"{item.get('id')} | {item.get('name')} | {item.get('provider')}"
            self.model_list.insert(tk.END, label)
            if item.get("id") == active_model:
                index = self.model_list.size() - 1
                self.model_list.selection_set(index)
                self.model_list.see(index)

    def select_model(self, _event: Any = None) -> None:
        selection = self.model_list.curselection()
        if not selection:
            return
        model_id = self.model_list.get(selection[0]).split(" | ", 1)[0]

        def task() -> None:
            self.client.request("POST", "/config/model", {"model": model_id})
            self.config_data = self.client.request("GET", "/config")
            self.after(0, self._render_config)

        self._run_async("切换模型中...", task)

    def save_ai_enabled(self) -> None:
        enabled = self.ai_enabled_var.get()

        def task() -> None:
            self.client.request("POST", "/config/ai", {"enabled": enabled})
            self.config_data = self.client.request("GET", "/config")

        self._run_async("保存 AI 开关中...", task)

    def save_scenario(self) -> None:
        source_id = self.source_var.get().split(" | ", 1)[0] if self.source_var.get() else None
        payload = {
            "system_prompt": self.prompt_text.get("1.0", tk.END).strip(),
            "temperature": round(float(self.temperature_var.get()), 2),
            "knowledge_strict": self.strict_var.get(),
        }
        if source_id:
            payload["knowledge_source_id"] = source_id

        def task() -> None:
            self.client.request("PUT", "/config/scenarios/default", payload)
            self.config_data = self.client.request("GET", "/config")
            self.after(0, self._render_config)

        self._run_async("保存策略中...", task)

    def send_chat(self) -> None:
        question = self.question_text.get("1.0", tk.END).strip()
        if not question:
            return
        self.answer_text.delete("1.0", tk.END)
        self.answer_text.insert(tk.END, "请求中...\n")

        def task() -> None:
            payload = {
                "teacher_id": self.session.username,
                "messages": [{"role": "user", "content": question}],
            }
            data = self.client.request("POST", "/chat", payload, auth=False, timeout=120)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            self.after(0, lambda: self._set_answer(content or json.dumps(data, ensure_ascii=False, indent=2)))

        self._run_async("聊天测试中...", task)

    def _set_answer(self, content: str) -> None:
        self.answer_text.delete("1.0", tk.END)
        self.answer_text.insert(tk.END, content)

    def _api_example(self, teacher_id: str) -> str:
        payload = {
            "teacher_id": teacher_id or "你的教师用户名",
            "messages": [{"role": "user", "content": "学生问题"}],
        }
        return (
            f'fetch("{self.session.base_url.rstrip("/")}/chat/stream", {{\n'
            '  method: "POST",\n'
            '  headers: {"Content-Type": "application/json", "Accept": "text/event-stream"},\n'
            f"  body: JSON.stringify({json.dumps(payload, ensure_ascii=False, indent=2)})\n"
            "});"
        )

    def copy_api_example(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._api_example(self.session.username))
        self._set_status("已复制 API 示例")


if __name__ == "__main__":
    app = EduGateDesktop()
    app.mainloop()
