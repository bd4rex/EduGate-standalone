from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from importlib.util import find_spec
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk


logger = logging.getLogger(__name__)

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
BACKEND_DIR = RESOURCE_DIR / "backend"
FRONTEND_DIR = RESOURCE_DIR / "frontend"
DATA_DIR = Path(
    os.getenv("EDUGATE_DATA_DIR")
    or Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home()) / "EduGate"
)
BACKEND_PORT = int(os.getenv("EDUGATE_BACKEND_PORT", "8000"))
AUTO_OPEN_ADMIN = os.getenv("EDUGATE_AUTO_OPEN", "true").lower() in {"1", "true", "yes", "on"}
REQUIRED_BACKEND_MODULES = ("fastapi", "httpx", "uvicorn", "pydantic", "multipart", "pypdf")


class EduGateStandalone(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EduGate 单机课堂版")
        self.geometry("860x570")
        self.minsize(780, 530)

        self.server = None
        self.server_thread: threading.Thread | None = None
        self.health_check_running = False
        self.start_watch_running = False
        self.admin_opened = False
        self.local_ip = detect_local_ip()
        self.backend_url = f"http://{self.local_ip}:{BACKEND_PORT}"
        self.admin_url = f"http://127.0.0.1:{BACKEND_PORT}/admin.html"
        self.student_url_var = tk.StringVar(value=f"{self.backend_url}/student.html")

        self._build_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(300, self.start_services)
        self.after(1200, self.refresh_status)

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
        style.configure("Hint.TLabel", foreground="#607089")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", padding=(14, 8))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="EduGate 单机课堂版", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="正在启动")
        ttk.Label(header, textvariable=self.status_var, style="Hint.TLabel").grid(row=0, column=1, sticky="e")

        panel = ttk.Frame(root, style="Panel.TFrame", padding=14)
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="教师控制台", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(panel, text=self.admin_url, style="Panel.TLabel").grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(panel, text="学生课堂链接", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(panel, textvariable=self.student_url_var, style="Panel.TLabel").grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(panel, text="运行数据", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Label(panel, text=str(DATA_DIR), style="Panel.TLabel").grid(row=2, column=1, sticky="w", pady=4)

        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ttk.Button(actions, text="启动服务", style="Primary.TButton", command=self.start_services).pack(side=tk.LEFT)
        ttk.Button(actions, text="停止服务", command=self.stop_services).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="打开教师控制台", command=self.open_admin).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="复制学生课堂链接", command=self.copy_student_url).pack(side=tk.LEFT, padx=8)

        log_panel = ttk.Frame(root, style="Panel.TFrame", padding=10)
        log_panel.grid(row=3, column=0, sticky="nsew")
        log_panel.rowconfigure(1, weight=1)
        log_panel.columnconfigure(0, weight=1)
        ttk.Label(log_panel, text="运行日志", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_panel, height=16, wrap=tk.WORD)
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def start_services(self) -> None:
        if self.server_thread and self.server_thread.is_alive():
            return
        ensure_runtime_environment()
        missing = missing_backend_modules()
        if missing:
            install_script = RESOURCE_DIR / "desktop" / "install_backend_deps.bat"
            messagebox.showwarning(
                "EduGate",
                f"后端依赖尚未安装：{', '.join(missing)}\n\n请先运行：\n{install_script}",
            )
            self.status_var.set("缺少后端依赖")
            return
        if healthcheck(f"http://127.0.0.1:{BACKEND_PORT}/health"):
            messagebox.showerror("EduGate", f"端口 {BACKEND_PORT} 已有服务运行，请先关闭后再启动 EduGate。")
            return
        try:
            sys.path.insert(0, str(BACKEND_DIR))
            from dotenv import load_dotenv

            load_dotenv(DATA_DIR / ".env", override=True)
            os.environ["EDUGATE_DATA_DIR"] = str(DATA_DIR)
            os.environ["EDUGATE_FRONTEND_DIR"] = str(FRONTEND_DIR)
            import uvicorn
            from app.config import settings
            from app.main import app, classroom_access

            config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=BACKEND_PORT,
                log_level="info",
                log_config=None,
                access_log=False,
            )
            self.server = uvicorn.Server(config)
            self.server_thread = threading.Thread(target=self._run_server, daemon=True)
            self.server_thread.start()
            token = classroom_access.token()
            self.student_url_var.set(
                f"{self.backend_url}/student.html#teacher_id={settings.admin_username}&class_token={token}"
            )
            self.log("EduGate 服务启动中，课堂 token 已生成。")
            self.status_var.set("服务启动中")
            self.start_watch_running = True
            threading.Thread(target=self._wait_until_ready, daemon=True).start()
        except Exception as exc:
            logger.exception("EduGate startup failed")
            self.log(f"启动失败：{type(exc).__name__}: {exc}")
            messagebox.showerror("EduGate", f"启动失败：{exc}")

    def _run_server(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:
            logger.exception("EduGate server thread stopped unexpectedly")
            self.log(f"服务线程异常：{type(exc).__name__}: {exc}")
            self.after(0, lambda: self.status_var.set("启动失败"))

    def stop_services(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        self.server = None
        self.server_thread = None
        self.admin_opened = False
        self.status_var.set("已停止")
        self.log("EduGate 服务已停止。")

    def refresh_status(self) -> None:
        if not self.health_check_running:
            self.health_check_running = True
            threading.Thread(target=self._check_status, daemon=True).start()
        self.after(2000, self.refresh_status)

    def _check_status(self) -> None:
        running = healthcheck(f"http://127.0.0.1:{BACKEND_PORT}/health")
        self.after(0, lambda: self.status_var.set("运行中" if running else "未运行"))
        self.health_check_running = False

    def _wait_until_ready(self) -> None:
        for _ in range(80):
            if healthcheck(f"http://127.0.0.1:{BACKEND_PORT}/health"):
                self.after(0, self._on_server_ready)
                self.start_watch_running = False
                return
            if self.server_thread and not self.server_thread.is_alive():
                break
            time.sleep(0.25)
        self.start_watch_running = False
        self.log("服务未能在 20 秒内启动，请查看上方错误信息。")

    def _on_server_ready(self) -> None:
        self.status_var.set("运行中")
        self.log("EduGate 已就绪。教师控制台与学生页面共用 8000 端口。")
        if AUTO_OPEN_ADMIN and not self.admin_opened:
            self.admin_opened = True
            webbrowser.open(self.admin_url)

    def open_admin(self) -> None:
        webbrowser.open(self.admin_url)

    def copy_student_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.student_url_var.get())
        self.status_var.set("已复制学生课堂链接")

    def log(self, message: str) -> None:
        logger.info(message)
        timestamp = time.strftime("%H:%M:%S")
        self.after(0, lambda: self._append_log(f"[{timestamp}] {message}\n"))

    def _append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def close(self) -> None:
        self.stop_services()
        self.destroy()


def ensure_runtime_environment() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env_path = DATA_DIR / ".env"
    example_path = BACKEND_DIR / ".env.example"
    if not env_path.exists() and example_path.exists():
        shutil.copyfile(example_path, env_path)


def missing_backend_modules() -> list[str]:
    return [module for module in REQUIRED_BACKEND_MODULES if find_spec(module) is None]


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def healthcheck(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


if __name__ == "__main__":
    ensure_runtime_environment()
    logging.basicConfig(
        level=logging.INFO,
        filename=DATA_DIR / "launcher.log",
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = EduGateStandalone()
    app.mainloop()
