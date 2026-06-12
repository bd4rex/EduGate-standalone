from __future__ import annotations

import os
import shutil
import socket
import subprocess
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


CODE_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = CODE_DIR / "backend"
FRONTEND_DIR = CODE_DIR / "frontend"
BACKEND_PORT = int(os.getenv("EDUGATE_BACKEND_PORT", "8000"))
FRONTEND_PORT = int(os.getenv("EDUGATE_FRONTEND_PORT", "8080"))
REQUIRED_BACKEND_MODULES = ("fastapi", "httpx", "uvicorn", "pydantic", "multipart", "pypdf")


class ManagedProcess:
    def __init__(self, name: str, command: list[str], cwd: Path, on_line) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.on_line = on_line
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self.is_running():
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        threading.Thread(target=self._read_output, daemon=True).start()

    def stop(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _read_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.on_line(self.name, line.rstrip())
        code = self.process.poll()
        self.on_line(self.name, f"process exited with code {code}")


class EduGateStandalone(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EduGate 单机课堂版")
        self.geometry("820x560")
        self.minsize(760, 520)

        self.backend: ManagedProcess | None = None
        self.frontend: ManagedProcess | None = None
        self.local_ip = detect_local_ip()
        self.backend_url = f"http://{self.local_ip}:{BACKEND_PORT}"
        self.admin_url = f"http://{self.local_ip}:{FRONTEND_PORT}/admin.html"

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
        ttk.Label(panel, text="学生 API", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(panel, text=self.backend_url, style="Panel.TLabel").grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(panel, text="默认登录", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Label(panel, text="admin / edugate", style="Panel.TLabel").grid(row=2, column=1, sticky="w", pady=4)

        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ttk.Button(actions, text="启动服务", style="Primary.TButton", command=self.start_services).pack(side=tk.LEFT)
        ttk.Button(actions, text="停止服务", command=self.stop_services).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="打开教师控制台", command=self.open_admin).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="复制学生 API 地址", command=self.copy_backend_url).pack(side=tk.LEFT, padx=8)

        log_panel = ttk.Frame(root, style="Panel.TFrame", padding=10)
        log_panel.grid(row=3, column=0, sticky="nsew")
        log_panel.rowconfigure(1, weight=1)
        log_panel.columnconfigure(0, weight=1)
        ttk.Label(log_panel, text="运行日志", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_panel, height=16, wrap=tk.WORD)
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def start_services(self) -> None:
        ensure_env_file()
        missing = missing_backend_modules()
        if missing:
            install_script = Path(__file__).resolve().parent / "install_backend_deps.bat"
            messagebox.showwarning(
                "EduGate",
                "后端依赖尚未安装：\n"
                f"{', '.join(missing)}\n\n"
                "请先运行：\n"
                f"{install_script}",
            )
            self.log("launcher", f"missing backend modules: {', '.join(missing)}")
            self.status_var.set("缺少后端依赖")
            return
        python = sys.executable
        if not (BACKEND_DIR / "app" / "main.py").exists():
            messagebox.showerror("EduGate", f"找不到后端目录：{BACKEND_DIR}")
            return
        if not (FRONTEND_DIR / "admin.html").exists():
            messagebox.showerror("EduGate", f"找不到教师控制台：admin.html")
            return

        self.backend = self.backend or ManagedProcess(
            "backend",
            [
                python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(BACKEND_PORT),
                "--env-file",
                ".env",
            ],
            BACKEND_DIR,
            self.log,
        )
        self.frontend = self.frontend or ManagedProcess(
            "frontend",
            [python, "-m", "http.server", str(FRONTEND_PORT), "--bind", "0.0.0.0"],
            FRONTEND_DIR,
            self.log,
        )

        try:
            self.backend.start()
            self.frontend.start()
            self.status_var.set("服务启动中")
        except FileNotFoundError as exc:
            messagebox.showerror("EduGate", f"启动失败：{exc}")
        except Exception as exc:
            messagebox.showerror("EduGate", f"启动失败：{exc}")

    def stop_services(self) -> None:
        if self.frontend:
            self.frontend.stop()
        if self.backend:
            self.backend.stop()
        self.status_var.set("已停止")

    def refresh_status(self) -> None:
        backend_ok = healthcheck(f"http://127.0.0.1:{BACKEND_PORT}/health")
        frontend_ok = bool(self.frontend and self.frontend.is_running())
        if backend_ok and frontend_ok:
            self.status_var.set("运行中")
        elif self.backend and self.backend.is_running():
            self.status_var.set("后端启动中")
        else:
            self.status_var.set("未运行")
        self.after(2000, self.refresh_status)

    def open_admin(self) -> None:
        webbrowser.open(self.admin_url)

    def copy_backend_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.backend_url)
        self.status_var.set("已复制学生 API 地址")

    def log(self, source: str, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.after(0, lambda: self._append_log(f"[{timestamp}] {source}: {line}\n"))

    def _append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def close(self) -> None:
        self.stop_services()
        self.destroy()


def ensure_env_file() -> None:
    env_path = BACKEND_DIR / ".env"
    example_path = BACKEND_DIR / ".env.example"
    if env_path.exists() or not example_path.exists():
        return
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
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


if __name__ == "__main__":
    app = EduGateStandalone()
    app.mainloop()
