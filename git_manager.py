"""图形化 Git 管理软件（Git Manager GUI）。

把常用 git 指令封装成按钮，支持：
  - 打开本地仓库 / 克隆 GitHub 仓库（在地址栏输入本地路径或 https://github.com/... 网址）
  - 常用操作：状态、添加、提交、推送、拉取、历史、分支切换、暂存、查看差异
  - GitHub 提交：配置 Token 后即可 commit + push 到 GitHub
  - 版本回溯：在历史列表中选择某个版本，可软/硬回溯或检出到新分支
  - 实体仓库文件结构树（带变更标记），右侧输出控制台显示每次 git 命令结果

运行方式：
    python git_manager.py                 # 打开图形界面
    python git_manager.py --selftest      # 无界面自测核心 git 操作（返回码 0/1）

依赖：Python 3.8+（标准库 tkinter），系统已安装 git 且在 PATH 中。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_NAME = "Git 管理器"
GIT_TIMEOUT = 300                                   # 网络操作超时（秒）
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # Windows 下不弹黑窗


def _config_path() -> Path:
    """配置文件路径；可用环境变量 GITGUI_CONFIG 覆盖（便携/受限环境）。"""
    override = os.environ.get("GITGUI_CONFIG")
    return Path(override) if override else Path.home() / ".gitgui_config.json"


CONFIG_FILE = _config_path()   # 保存 GitHub Token 等配置

# 变更状态 -> (显示文字, 颜色) 的映射，用于文件树和变更列表
STATUS_STYLE = {
    "M":  ("已修改", "#d97706"),   # 已修改（工作区）
    "A":  ("已添加", "#16a34a"),   # 已暂存新增
    "D":  ("已删除", "#dc2626"),   # 已删除
    "R":  ("已重命名", "#9333ea"),
    "C":  ("已复制", "#9333ea"),
    "??": ("未跟踪", "#2563eb"),   # 未跟踪新文件
    "U":  ("冲突", "#dc2626"),     # 合并冲突
}


# ---------------------------------------------------------------------------
# 底层工具函数
# ---------------------------------------------------------------------------
def decode_output(data: bytes) -> str:
    """把 git 输出的字节解码成字符串，兼容 UTF-8 与 GBK 环境。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_git_result(result) -> tuple[int, str, str]:
    """把 git 操作结果统一成 (返回码, 输出, 错误)。

    底层函数有两种返回风格，后台任务统一按三元素处理：
      - run() 系列：            (返回码, stdout, stderr)   三元素
      - commit_and_push()/push()：(是否成功, 说明)         两元素
    """
    if len(result) == 3:
        return result
    ok, msg = result
    return (0, msg, "") if ok else (1, "", msg)


def load_config() -> dict:
    """读取配置文件（不存在则返回空字典）。"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict) -> bool:
    """把配置写入文件；失败返回 False（不影响主流程，仅持久化失效）。"""
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        return True
    except OSError:
        return False


def is_git_url(text: str) -> bool:
    """判断地址栏输入是否为 git 远程地址（网址或 git@ 形式）。"""
    return bool(re.match(r"^(https?://|git@|ssh://|git://)", text.strip()))


# ---------------------------------------------------------------------------
# git 命令封装
# ---------------------------------------------------------------------------
class Git:
    """对 git 命令的薄封装：统一处理编码、超时、报错提示。"""

    def __init__(self, repo_dir: Path | None = None, token: str = ""):
        self.repo_dir = repo_dir
        self.token = token  # GitHub Token，用于 https 推送时自动注入

    # -- 基础命令执行 ------------------------------------------------------
    def run(self, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
        """执行 git 命令，返回 (返回码, 标准输出, 标准错误)。

        注意：-C（指定目录）和 -c（配置）都是全局选项，必须放在子命令之前。
        """
        cmd = ["git"]
        if self.repo_dir is not None:
            cmd += ["-C", str(self.repo_dir)]
        # http.connectTimeout / lowSpeed*：网络卡顿时约 30 秒内报错，
        # 避免推送/拉取长时间"没反应"（默认可能挂 5 分钟）
        cmd += ["-c", "core.quotepath=false",
                "-c", "http.connectTimeout=30",
                "-c", "http.lowSpeedLimit=1000",
                "-c", "http.lowSpeedTime=30",
                *args]
        try:
            # GIT_TERMINAL_PROMPT=0 + 无标准输入：禁止 git 交互式询问用户名/密码。
            # 否则推送 https 远程且未配置凭据时，git 会阻塞在等输入上，界面表现为"点了没反应"。
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL, env=env,
            )
        except subprocess.TimeoutExpired:
            return 1, "", f"命令超时（>{timeout}s）：git {' '.join(args)}"
        except FileNotFoundError:
            return 1, "", "未找到 git 命令，请先安装 Git 并加入 PATH（https://git-scm.com）"
        except OSError as exc:
            return 1, "", f"执行 git 失败：{exc}"
        out = decode_output(proc.stdout)
        err = decode_output(proc.stderr)
        return proc.returncode, out, err

    def ok(self, *args: str) -> tuple[bool, str, str]:
        """执行命令并返回是否成功，供 UI 直接判断。"""
        code, out, err = self.run(*args)
        return code == 0, out, err

    # -- 常用操作 ----------------------------------------------------------
    def status(self) -> list[tuple[str, str]]:
        """返回变更列表 [(状态码XY, 相对路径)]。"""
        code, out, _ = self.run("status", "--porcelain", "-uall")
        if code != 0:
            return []
        items = []
        for line in out.splitlines():
            if not line.strip():
                continue
            xy, path = line[:2], line[3:].strip()
            if xy[0] == "R" and " -> " in path:  # 重命名：只取新路径
                path = path.split(" -> ")[-1]
            items.append((xy.strip(), path))
        return items

    def current_branch(self) -> str:
        """当前分支名；处于游离 HEAD 时返回提交短哈希。"""
        code, out, _ = self.run("rev-parse", "--abbrev-ref", "HEAD")
        if code == 0 and out.strip() and out.strip() != "HEAD":
            return out.strip()
        code2, out2, _ = self.run("rev-parse", "--short", "HEAD")
        return out2.strip() if code2 == 0 else "(无提交)"

    def branches(self) -> list[tuple[str, str, str]]:
        """返回分支列表 [(分支名, 是否当前, 最后提交说明)]。"""
        code, out, _ = self.run("branch", "--format=%(refname:short)|%(subject)")
        if code != 0:
            return []
        current = self.current_branch()
        result = []
        for line in out.splitlines():
            if not line.strip():
                continue
            name, _, subject = line.partition("|")
            result.append((name, name == current, subject))
        return result

    def commits(self, limit: int = 200) -> list[tuple[str, str, str, str]]:
        """返回提交历史 [(短哈希, 日期, 作者, 说明)]，按时间倒序。"""
        fmt = "%h%x1f%ad%x1f%an%x1f%s"
        code, out, _ = self.run("log", f"-n {limit}",
                                f"--pretty=format:{fmt}", "--date=format:%Y-%m-%d %H:%M")
        if code != 0:
            return []
        result = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 4:
                result.append((parts[0], parts[1], parts[2], parts[3]))
        return result

    def remotes(self) -> list[tuple[str, str]]:
        """返回远程仓库 [(名称, URL)]。"""
        code, out, _ = self.run("remote", "-v")
        if code != 0:
            return []
        seen = set()
        result = []
        for line in out.splitlines():
            name, _, url = line.partition("\t")
            url = url.split(" ")[0]
            if (name, url) not in seen:
                seen.add((name, url))
                result.append((name, url))
        return result

    def auth_url(self, url: str) -> str:
        """在 https 远程地址中注入 Token，用于推送/拉取认证。"""
        if not self.token or not url.startswith("https://"):
            return url
        # https://github.com/xxx/repo.git -> https://<token>@github.com/xxx/repo.git
        return url.replace("https://", f"https://{self.token}@", 1)

    # -- 便捷组合操作 ------------------------------------------------------
    def commit_and_push(self, message: str, author: str = "",
                        push: bool = False, auto_add: bool = False) -> tuple[bool, str]:
        """提交（可选先暂存、可选推送）。返回 (是否成功, 结果说明)。"""
        if not message.strip():
            return False, "提交信息不能为空"
        if auto_add:
            code, _, err = self.run("add", "-A")
            if code != 0:
                return False, f"自动暂存失败：{err}"
        args = ["commit", "-m", message.strip()]
        if author.strip():
            args += ["--author", author.strip()]
        code, out, err = self.run(*args)
        if code != 0:
            return False, err or out
        if not push:
            return True, out or "提交成功"
        return self.push()

    def push(self) -> tuple[bool, str]:
        """推送到远程（自动注入 Token）。"""
        remotes = self.remotes()
        if not remotes:
            return False, "没有配置远程仓库，无法推送"
        name, url = remotes[0]
        # 用 symbolic-ref 区分：游离 HEAD（失败） vs 在某分支上（成功）
        code, sym, _ = self.run("symbolic-ref", "-q", "HEAD")
        if code != 0:
            return False, ("当前处于游离 HEAD（未在任何分支上），无法推送。\n"
                           "提示：请先在「分支」页签切换到一个分支，再点「推送」。")
        # 分支上但还没有任何提交（unborn）：HEAD 符号引用存在，但指向的 ref 不存在
        code2, _, _ = self.run("rev-parse", "--verify", "HEAD")
        if code2 != 0:
            return False, ("当前分支还没有任何提交，无法推送。\n"
                           "提示：请先「添加全部」→「提交…」创建第一次提交，再点「推送」。")
        branch = sym.strip().removeprefix("refs/heads/")
        code, out, err = self.run("push", self.auth_url(url),
                                  f"HEAD:{branch}")
        if code == 0:
            return True, out or f"已推送到 {name}"
        if ("Authentication failed" in err or "401" in err or "403" in err
                or "could not read Username" in err
                or "terminal prompts disabled" in err
                or "authentication failed" in err.lower()):
            return False, (err + "\n提示：认证失败，请在「Token 设置」中配置 GitHub Token。")
        return False, err or out


# ---------------------------------------------------------------------------
# 后台线程执行器：让 git 命令在子线程跑，不阻塞界面
# ---------------------------------------------------------------------------
class Worker(threading.Thread):
    """在后台线程执行一个函数，结果通过队列送回主线程。"""

    def __init__(self, queue_: queue.Queue, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self._queue = queue_
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
            self._queue.put(("ok", result))
        except Exception as exc:  # noqa: BLE001 —— 后台线程异常统一上报
            self._queue.put(("error", exc))


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class App(tk.Tk):
    """Git 管理器主窗口。"""

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x720")
        self.minsize(960, 600)

        self.cfg = load_config()
        self.repo_dir: Path | None = None      # 当前打开的仓库目录
        self.git: Git | None = None
        self.busy = False                      # 是否有后台任务在跑
        self._queue: queue.Queue = queue.Queue()

        # 窗口获得焦点时自动刷新（比如在编辑器改完文件切回本软件）
        self._last_auto_refresh = 0.0
        self.bind("<FocusIn>", self._on_focus_refresh)

        self._build_style()
        self._build_ui()
        self._poll_queue()
        self._print("提示：请在地址栏输入本地仓库路径，或粘贴 GitHub 仓库地址（将自动克隆）。")

        # 启动时自动打开上次的仓库（先校验它仍是有效仓库，避免弹出无谓的对话框）
        last = self.cfg.get("last_repo")
        if last and Path(last).is_dir():
            probe = Git(repo_dir=Path(last))
            code, _, _ = probe.run("rev-parse", "--is-inside-work-tree")
            if code == 0:
                self.addr_var.set(last)
                self.open_repo(Path(last), quiet=True)

    # ------------------------------------------------------------------ UI 构建
    def _build_style(self) -> None:
        """统一样式：字体、颜色，让界面简洁美观。"""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        font = ("Segoe UI", 10)
        self.option_add("*Font", font)
        style.configure(".", font=font)
        style.configure("TButton", padding=(10, 4))
        style.configure("Toolbar.TFrame", background="#f3f4f6")
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 6))
        style.configure("Console.TFrame", background="#1e1e1e")

    def _build_ui(self) -> None:
        # -- 第 1 行：仓库地址栏 + 打开/克隆/Token/刷新 ---------------------
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="仓库地址：").pack(side="left")
        self.addr_var = tk.StringVar()
        addr = ttk.Entry(toolbar, textvariable=self.addr_var, width=46)
        addr.pack(side="left", fill="x", expand=True, padx=(0, 6))
        addr.bind("<Return>", lambda e: self.on_open_or_clone())
        ttk.Button(toolbar, text="打开 / 克隆", command=self.on_open_or_clone).pack(side="left", padx=2)
        ttk.Button(toolbar, text="初始化仓库", command=self.on_init).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Token 设置", command=self.on_token_settings).pack(side="left", padx=2)

        # -- 第 2 行：常用 git 操作按钮 ------------------------------------
        actions = ttk.Frame(self, padding=(8, 2))
        actions.pack(fill="x")
        self.buttons: dict[str, ttk.Button] = {}
        specs = [
            ("refresh", "🔄 刷新", self.on_refresh),
            ("status", "状态", self.on_status),
            ("add", "添加全部", self.on_add_all),
            ("commit", "提交…", self.on_commit),
            ("push", "推送", self.on_push),
            ("pull", "拉取", self.on_pull),
            ("rollback", "版本回溯", self.on_rollback),
            ("branch", "切换分支", self.on_switch_branch),
            ("stash", "暂存", self.on_stash),
            ("unstash", "恢复暂存", self.on_unstash),
            ("diff", "查看差异", self.on_diff),
        ]
        for key, text, cmd in specs:
            btn = ttk.Button(actions, text=text, command=cmd)
            btn.pack(side="left", padx=2)
            self.buttons[key] = btn

        # -- 中部：左侧文件树 + 右侧多页签 ----------------------------------
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=6)

        # 左：实体仓库文件结构
        left = ttk.Frame(paned)
        ttk.Label(left, text="📁 仓库文件结构（●修改  +新增  ×删除  ○未跟踪）",
                  font=("Segoe UI", 9)).pack(anchor="w", padx=4, pady=(2, 0))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("dir", foreground="#374151")
        self.tree.tag_configure("M", foreground="#d97706")
        self.tree.tag_configure("A", foreground="#16a34a")
        self.tree.tag_configure("D", foreground="#dc2626")
        self.tree.tag_configure("??", foreground="#2563eb")
        self.tree.tag_configure("R", foreground="#9333ea")
        paned.add(left, weight=1)

        # 右：页签（变更 / 历史 / 分支 / 差异）
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        # 页签 1：变更列表
        tab_status = ttk.Frame(nb)
        self.status_tree = ttk.Treeview(tab_status,
                                        columns=("st", "file"), show="headings")
        self.status_tree.heading("st", text="状态")
        self.status_tree.heading("file", text="文件")
        self.status_tree.column("st", width=90, anchor="center")
        self.status_tree.column("file", width=380)
        self.status_tree.pack(fill="both", expand=True, padx=4, pady=4)
        for tag, (_, color) in STATUS_STYLE.items():
            self.status_tree.tag_configure(tag, foreground=color)
        row = ttk.Frame(tab_status)
        row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(row, text="添加所选", command=self.on_add_selected).pack(side="left", padx=2)
        ttk.Button(row, text="放弃所选修改", command=self.on_discard_selected).pack(side="left", padx=2)
        nb.add(tab_status, text="变更")

        # 页签 2：提交历史（版本回溯入口）
        tab_log = ttk.Frame(nb)
        self.log_tree = ttk.Treeview(tab_log,
                                     columns=("hash", "date", "author", "msg"),
                                     show="headings")
        self.log_tree.heading("hash", text="版本号")
        self.log_tree.heading("date", text="日期")
        self.log_tree.heading("author", text="作者")
        self.log_tree.heading("msg", text="提交说明")
        self.log_tree.column("hash", width=80, anchor="center")
        self.log_tree.column("date", width=120, anchor="center")
        self.log_tree.column("author", width=100)
        self.log_tree.column("msg", width=300)
        self.log_tree.pack(fill="both", expand=True, padx=4, pady=4)
        row2 = ttk.Frame(tab_log)
        row2.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(row2, text="⬅ 回溯到所选版本",
                   command=self.on_rollback).pack(side="left", padx=2)
        ttk.Label(row2, text="提示：选中历史中的某个版本后点击「回溯」",
                  foreground="#6b7280").pack(side="left", padx=8)
        nb.add(tab_log, text="提交历史")

        # 页签 3：分支
        tab_branch = ttk.Frame(nb)
        self.branch_tree = ttk.Treeview(tab_branch,
                                        columns=("cur", "name", "msg"), show="headings")
        self.branch_tree.heading("cur", text="当前")
        self.branch_tree.heading("name", text="分支名")
        self.branch_tree.heading("msg", text="最后提交")
        self.branch_tree.column("cur", width=60, anchor="center")
        self.branch_tree.column("name", width=160)
        self.branch_tree.column("msg", width=330)
        self.branch_tree.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(tab_branch, text="切换到选中分支",
                   command=self.on_switch_branch).pack(anchor="w", padx=4, pady=(0, 4))
        nb.add(tab_branch, text="分支")

        # 页签 4：差异查看
        tab_diff = ttk.Frame(nb)
        self.diff_text = tk.Text(tab_diff, wrap="none", state="disabled",
                                 bg="#1e1e1e", fg="#d4d4d4",
                                 font=("Consolas", 10), relief="flat")
        diff_scroll = ttk.Scrollbar(tab_diff, command=self.diff_text.yview)
        self.diff_text.configure(yscrollcommand=diff_scroll.set)
        self.diff_text.pack(side="left", fill="both", expand=True)
        diff_scroll.pack(side="right", fill="y")
        self.diff_text.tag_configure("add", foreground="#7ee787")
        self.diff_text.tag_configure("del", foreground="#ff7b72")
        self.diff_text.tag_configure("hdr", foreground="#79c0ff")
        nb.add(tab_diff, text="差异")

        # -- 底部：输出控制台 + 状态栏 --------------------------------------
        console_frame = ttk.Frame(self)
        console_frame.pack(fill="both", padx=8, pady=(0, 4))
        self.console = tk.Text(console_frame, height=9, wrap="none",
                               state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                               font=("Consolas", 9), relief="flat")
        cscroll = ttk.Scrollbar(console_frame, command=self.console.yview)
        self.console.configure(yscrollcommand=cscroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        cscroll.pack(side="right", fill="y")
        self.console.tag_configure("cmd", foreground="#79c0ff")
        self.console.tag_configure("ok", foreground="#7ee787")
        self.console.tag_configure("err", foreground="#ff7b72")
        self.console.tag_configure("info", foreground="#d2a8ff")

        self.statusbar = ttk.Label(self, relief="sunken", anchor="w", padding=(6, 2))
        self.statusbar.pack(fill="x")

    # ------------------------------------------------------------------ 打印与状态
    def _print(self, text: str, tag: str = "info") -> None:
        """向底部控制台追加一行文字。"""
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n", (tag,))
        self.console.configure(state="disabled")
        self.console.see("end")

    def _set_status(self, text: str) -> None:
        """更新底部状态栏。"""
        branch = self.git.current_branch() if self.git else "-"
        self.statusbar.configure(text=f"分支：{branch}　|　{text}")

    def _set_busy(self, busy: bool) -> None:
        """忙时禁用全部操作按钮，防止同时跑多个 git 命令。"""
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in self.buttons.values():
            btn.configure(state=state)

    def _poll_queue(self) -> None:
        """定期把后台线程的结果取回主线程处理。

        用 try/finally 保证轮询永远继续：任何一条消息处理出错都不能
        让界面卡死（否则 busy 会一直为 True，按钮全部失灵）。
        """
        try:
            try:
                while True:
                    kind, payload = self._queue.get_nowait()
                    if kind == "error":
                        self._print(f"后台任务异常：{payload}", "err")
                        self._set_status("任务异常")
                        self._set_busy(False)
                    else:
                        try:
                            self._dispatch(payload)
                        except Exception as exc:  # noqa: BLE001
                            self._print(f"界面刷新异常：{exc}", "err")
                            self._set_busy(False)
            except queue.Empty:
                pass
        finally:
            self.after(60, self._poll_queue)

    def _dispatch(self, payload: dict) -> None:
        """按 action 字段把后台结果分发给对应收尾函数。"""
        self._set_busy(False)
        action = payload.get("action")
        if action == "open":
            self._finish_open(payload)
        elif action == "refresh":
            self._render(payload)
        elif action == "console":
            self._print(payload.get("text", ""), payload.get("tag", "info"))
            self._set_status(payload.get("status", "完成"))
            self._refresh_light()
        elif action == "diff":
            self._show_diff(payload)
        else:
            self._set_status("完成")

    # ------------------------------------------------------------------ 地址栏动作
    def on_open_or_clone(self) -> None:
        """地址栏内容：本地路径 -> 打开；网址 -> 克隆。"""
        text = self.addr_var.get().strip().strip('"')
        if not text:
            messagebox.showinfo(APP_NAME, "请输入本地仓库路径或 GitHub 仓库地址")
            return
        if is_git_url(text):
            self.on_clone(text)
        else:
            self.open_repo(Path(text))

    def open_repo(self, path: Path, quiet: bool = False) -> None:
        """打开本地仓库（在后台线程校验并刷新界面）。"""
        if self.busy:
            return
        path = path.expanduser().resolve()
        if not path.is_dir():
            if not quiet:
                messagebox.showerror(APP_NAME, f"目录不存在：{path}")
            return
        token = self.cfg.get("github_token", "")
        self.git = Git(repo_dir=path, token=token)
        self._open_quiet = quiet   # 静默打开（启动自动打开）时不弹询问框
        self._set_busy(True)
        self._print(f"$ 打开仓库 {path}", "cmd")
        Worker(self._queue, self._work_open, path).start()

    def _work_open(self, path: Path) -> dict:
        """后台线程：校验目录是否为 git 仓库。"""
        code, _, err = self.git.run("rev-parse", "--is-inside-work-tree")
        if code != 0:
            return {"action": "open", "ok": False, "error": err,
                    "path": path, "need_init": True}
        return {"action": "open", "ok": True, "path": path}

    def _finish_open(self, payload: dict) -> None:
        """打开结果收尾。"""
        if payload.get("need_init") and payload["path"].is_dir():
            self._print(f"提示：{payload['path']} 还不是 git 仓库。", "err")
            if getattr(self, "_open_quiet", False):
                # 启动时静默打开失败：只提示，不弹窗打扰
                self._set_status("上次的仓库已失效，请重新选择")
                return
            if messagebox.askyesno(APP_NAME, "该目录还不是 git 仓库，是否在此初始化？"):
                self.on_init()
            else:
                self._set_status("未打开仓库")
            return
        if not payload.get("ok"):
            self._print(payload.get("error", "打开失败"), "err")
            self._set_status("打开失败")
            self._set_busy(False)
            return
        self.repo_dir = payload["path"]
        self.addr_var.set(str(self.repo_dir))
        self.cfg["last_repo"] = str(self.repo_dir)
        if not save_config(self.cfg):
            self._print(f"提示：配置写入失败（{CONFIG_FILE}），仅本次会话记住仓库。", "err")
        self._refresh_all()

    def on_clone(self, url: str) -> None:
        """克隆 GitHub 仓库到本地（让用户选择保存位置）。"""
        if self.busy:
            return
        target = filedialog.askdirectory(title="选择克隆保存位置（将创建仓库子文件夹）")
        if not target:
            return
        name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"
        dest = Path(target) / name
        if dest.exists():
            messagebox.showerror(APP_NAME, f"目标文件夹已存在：{dest}")
            return
        token = self.cfg.get("github_token", "")
        auth_url = url
        if token and url.startswith("https://"):
            auth_url = url.replace("https://", f"https://{token}@", 1)
        self._set_busy(True)
        self._print(f"$ git clone {url}", "cmd")
        self._print("克隆中，请稍候……", "info")
        Worker(self._queue, self._work_clone, auth_url, dest).start()

    def _work_clone(self, url: str, dest: Path) -> dict:
        git = Git(token=self.cfg.get("github_token", ""))
        code, out, err = git.run("clone", url, str(dest))
        return {"action": "open", "ok": code == 0, "error": err or out,
                "path": dest, "need_init": False}

    def on_init(self) -> None:
        """在地址栏目录初始化 git 仓库。"""
        text = self.addr_var.get().strip().strip('"')
        if not text or is_git_url(text):
            messagebox.showinfo(APP_NAME, "请先输入要初始化仓库的本地文件夹路径")
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            messagebox.showerror(APP_NAME, f"目录不存在：{path}")
            return
        git = Git(repo_dir=path)
        code, _, err = git.run("init")
        if code != 0:
            messagebox.showerror(APP_NAME, f"初始化失败：{err}")
            return
        self._print(f"已初始化仓库：{path}", "ok")
        self.open_repo(path)

    def on_token_settings(self) -> None:
        """Token 设置对话框：保存 GitHub 个人访问令牌。"""
        dialog = tk.Toplevel(self)
        dialog.title("GitHub Token 设置")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("560x200")
        ttk.Label(dialog, text="GitHub 个人访问令牌（Personal Access Token）：").pack(anchor="w", padx=12, pady=(12, 2))
        token_var = tk.StringVar(value=self.cfg.get("github_token", ""))
        entry = ttk.Entry(dialog, textvariable=token_var, width=60, show="•")
        entry.pack(fill="x", padx=12)
        ttk.Label(dialog, foreground="#6b7280", wraplength=530, justify="left",
                  text="在 GitHub → Settings → Developer settings → Personal access tokens 生成，"
                       "勾选 repo 权限。Token 仅保存在本机 ~/.gitgui_config.json 中，推送时自动使用。").pack(anchor="w", padx=12, pady=6)
        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=12, pady=10)

        def save() -> None:
            token = token_var.get().strip()
            if token:
                self.cfg["github_token"] = token
            else:
                self.cfg.pop("github_token", None)
            ok = save_config(self.cfg)
            if self.git:
                self.git.token = token
            dialog.destroy()
            if ok:
                self._print("Token 已保存。" if token else "已清除 Token。", "ok")
            else:
                messagebox.showwarning(APP_NAME, f"配置保存失败：{CONFIG_FILE}")

        ttk.Button(btns, text="保存", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side="left", padx=4)

    # ------------------------------------------------------------------ 操作按钮
    def on_refresh(self) -> None:
        """刷新文件树、变更、历史、分支。"""
        if not self._require_repo():
            return
        self._refresh_all()

    def on_status(self) -> None:
        """查看状态：把 git status 完整输出打到控制台。"""
        if not self._require_repo():
            return
        self._run_console(lambda g: g.run("status", "-uall"),
                          "git status -uall", "状态")

    def on_add_all(self) -> None:
        """添加全部变更到暂存区。"""
        if not self._require_repo():
            return
        self._run_console(lambda g: g.run("add", "-A"), "git add -A", "添加全部")

    def on_add_selected(self) -> None:
        """添加「变更」页签中选中的文件。"""
        if not self._require_repo():
            return
        sel = self.status_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请在「变更」页签中先选择文件")
            return
        files = [self.status_tree.item(i, "values")[1] for i in sel]
        self._run_console(lambda g: g.run("add", "--", *files),
                          f"git add {' '.join(files)}", "添加所选")

    def on_discard_selected(self) -> None:
        """放弃所选文件的未提交修改（checkout -- 文件）。"""
        if not self._require_repo():
            return
        sel = self.status_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请在「变更」页签中先选择文件")
            return
        files = [self.status_tree.item(i, "values")[1] for i in sel]
        if not messagebox.askyesno(APP_NAME,
                                   "确定放弃所选文件的修改？\n（未暂存改动将丢失）\n\n" + "\n".join(files)):
            return
        self._run_console(lambda g: g.run("checkout", "--", *files),
                          f"git checkout -- {' '.join(files)}", "放弃修改")

    def on_commit(self) -> None:
        """打开提交对话框。"""
        if not self._require_repo():
            return
        CommitDialog(self)

    def on_push(self) -> None:
        """推送到远程。"""
        if not self._require_repo():
            return
        # 未配置 Token 且远程是 https 时，提前提示，避免用户以为没反应
        remotes = self.git.remotes()
        if (remotes and remotes[0][1].startswith("https://")
                and not self.cfg.get("github_token")):
            self._print("提示：远程是 https 且未配置 Token，推送会认证失败或弹出系统登录窗口。"
                        "建议先在「Token 设置」中配置 GitHub Token。", "err")
        self._run_console(lambda g: g.push(), "git push", "推送")

    def on_pull(self) -> None:
        """从远程拉取。"""
        if not self._require_repo():
            return
        def do_pull(g: Git):
            remotes = g.remotes()
            if not remotes:
                return False, "", "没有配置远程仓库，无法拉取"
            return g.run("pull", g.auth_url(remotes[0][1]))
        self._run_console(do_pull, "git pull", "拉取")

    def on_stash(self) -> None:
        """暂存当前所有改动（含未跟踪文件）。"""
        if not self._require_repo():
            return
        if not messagebox.askyesno(APP_NAME, "暂存当前所有改动？\n（工作区会变干净，可随时恢复）"):
            return
        self._run_console(lambda g: g.run("stash", "push", "-u"), "git stash push -u", "暂存")

    def on_unstash(self) -> None:
        """恢复最近一次暂存。"""
        if not self._require_repo():
            return
        self._run_console(lambda g: g.run("stash", "pop"), "git stash pop", "恢复暂存")

    def on_diff(self) -> None:
        """查看选中文件的差异（自动区分已暂存 / 未暂存 / 未跟踪）。"""
        if not self._require_repo():
            return
        sel = self.status_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请在「变更」页签中先选择文件")
            return
        path = self.status_tree.item(sel[0], "values")[1]
        # 查该文件的状态码：?? 未跟踪；首位非空格 = 已暂存；否则 = 仅未暂存
        xy = "  "
        for code, p in self.git.status():
            if p == path:
                xy = code
                break
        if xy == "??":
            # 未跟踪新文件：git diff 对它没有输出，改为预览文件内容
            self._print(f"$ 新文件（未跟踪）：{path}", "cmd")

            def preview(_g):
                try:
                    content = (self.repo_dir / path).read_text(
                        encoding="utf-8", errors="replace")
                    return (0, f"（新文件，尚未纳入版本管理，点「添加全部」后即可提交）\n\n{content}", "")
                except OSError as exc:
                    return (1, "", f"读取失败：{exc}")

            self._run_console(preview, f"新文件预览：{path}", "差异",
                              diff_only=True, file=path)
        else:
            # 已跟踪文件：diff HEAD 同时覆盖已暂存与未暂存的改动，预览最全
            self._run_console(lambda g: g.run("diff", "HEAD", "--", path),
                              f"git diff HEAD -- {path}", "差异", diff_only=True, file=path)

    def on_rollback(self) -> None:
        """版本回溯：把仓库恢复到历史中的某个版本。"""
        if not self._require_repo():
            return
        sel = self.log_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请在「提交历史」页签中先选择一个版本")
            return
        values = self.log_tree.item(sel[0], "values")
        target_hash, target_msg = values[0], values[3]
        dialog = tk.Toplevel(self)
        dialog.title("版本回溯")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("520x320")
        ttk.Label(dialog, wraplength=480, justify="left",
                  text=f"将回溯到版本 {target_hash}\n“{target_msg}”\n\n"
                       "请选择回溯方式：").pack(anchor="w", padx=14, pady=(14, 6))
        mode = tk.StringVar(value="new_branch")
        ttk.Radiobutton(dialog, text="① 检出到新分支（安全，推荐）—— 不丢任何东西，"
                                     "在当前版本创建新分支并切过去", variable=mode, value="new_branch").pack(anchor="w", padx=18, pady=3)
        ttk.Radiobutton(dialog, text="② 软回溯（保留工作区）—— 把 HEAD 指回该版本，"
                                     "之后的提交会留在暂存区", variable=mode, value="soft").pack(anchor="w", padx=18, pady=3)
        ttk.Radiobutton(dialog, text="③ 硬回溯（危险！）—— 丢弃该版本之后的所有提交和工作区改动，"
                                     "无法恢复", variable=mode, value="hard").pack(anchor="w", padx=18, pady=3)
        hint = ttk.Label(dialog, foreground="#dc2626", wraplength=480, justify="left",
                         text="警告：硬回溯会永久删除历史提交，请先确认已推送或备份！")
        hint.pack(anchor="w", padx=18, pady=6)
        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=14, pady=10)

        def do_rollback() -> None:
            m = mode.get()
            if m == "hard" and not messagebox.askyesno(
                    APP_NAME, "⚠ 硬回溯将永久删除该版本之后的提交与工作区改动，\n确定继续？"):
                return
            dialog.destroy()
            self._set_busy(True)
            if m == "new_branch":
                branch = f"rollback-{target_hash}"
                self._print(f"$ git checkout -b {branch} {target_hash}", "cmd")
                Worker(self._queue, self._work_rollback,
                       lambda g: g.run("checkout", "-b", branch, target_hash),
                       f"已检出新分支 {branch}（版本 {target_hash}）").start()
            elif m == "soft":
                self._print(f"$ git reset --soft {target_hash}", "cmd")
                Worker(self._queue, self._work_rollback,
                       lambda g: g.run("reset", "--soft", target_hash),
                       f"已软回溯到 {target_hash}（改动保留在暂存区）").start()
            else:
                self._print(f"$ git reset --hard {target_hash}", "cmd")
                Worker(self._queue, self._work_rollback,
                       lambda g: g.run("reset", "--hard", target_hash),
                       f"已硬回溯到 {target_hash}").start()

        ttk.Button(btns, text="执行回溯", command=do_rollback).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side="left", padx=4)

    def _work_rollback(self, fn, ok_text: str) -> dict:
        """后台线程执行回溯命令。"""
        code, out, err = normalize_git_result(fn(self.git))
        return {"action": "console", "text": (out or err) + ("" if code == 0 else ""),
                "tag": "ok" if code == 0 else "err",
                "status": ok_text if code == 0 else f"回溯失败：{err}"}

    def on_switch_branch(self) -> None:
        """切换分支。"""
        if not self._require_repo():
            return
        branches = self.git.branches()
        if not branches:
            messagebox.showinfo(APP_NAME, "仓库还没有分支")
            return
        dialog = tk.Toplevel(self)
        dialog.title("切换分支")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("420x140")
        ttk.Label(dialog, text="选择要切换到的分支：").pack(anchor="w", padx=14, pady=(14, 4))
        names = [b[0] for b in branches]
        var = tk.StringVar(value=self.git.current_branch())
        combo = ttk.Combobox(dialog, textvariable=var, values=names, state="readonly", width=40)
        combo.pack(fill="x", padx=14)
        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=14, pady=12)

        def switch() -> None:
            target = var.get()
            if not target or target == self.git.current_branch():
                dialog.destroy()
                return
            dialog.destroy()
            self._run_console(lambda g: g.run("checkout", target),
                              f"git checkout {target}", f"切换到 {target}")

        ttk.Button(btns, text="切换", command=switch).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side="left", padx=4)

    # ------------------------------------------------------------------ 后台任务通用工具
    def _require_repo(self) -> bool:
        """检查当前是否已打开仓库。"""
        if self.git is None or self.repo_dir is None:
            messagebox.showinfo(APP_NAME, "请先在地址栏打开或克隆一个仓库")
            return False
        return True

    def _run_console(self, fn, cmd_text: str, status_text: str,
                     diff_only: bool = False, file: str = "") -> None:
        """在后台跑一个 git 命令，把结果打到控制台/差异页，然后轻量刷新。"""
        if self.busy:
            return
        self._set_busy(True)
        self._print(f"$ {cmd_text}", "cmd")
        Worker(self._queue, self._work_console, fn, status_text,
               diff_only, file).start()

    def _work_console(self, fn, status_text: str, diff_only: bool, file: str) -> dict:
        """后台线程：执行操作并把结果转成界面可用的字典。

        注意：fn 的返回格式可能是三元素 (code, out, err) 或两元素 (ok, msg)，
        统一用 normalize_git_result 归一化，避免解包失败。
        """
        code, out, err = normalize_git_result(fn(self.git))
        if diff_only:
            return {"action": "diff", "code": code, "out": out, "err": err,
                    "file": file}
        text = out or err
        return {"action": "console", "text": text,
                "tag": "ok" if code == 0 else "err",
                "status": f"{status_text}完成" if code == 0 else f"{status_text}失败"}

    def _on_focus_refresh(self, _event=None) -> None:
        """窗口重新获得焦点时自动刷新一次。

        典型场景：用户在编辑器里改完文件，切回本软件 —— 无需手动点刷新，
        「变更」页签和文件树就能看到最新状态。
        用 1 秒节流 + busy 保护，避免连续弹窗/对话框关闭时触发刷新风暴。
        """
        if self.git is None or self.busy:
            return
        now = time.time()
        if now - self._last_auto_refresh < 1.0:
            return
        self._last_auto_refresh = now
        self._refresh_all()

    def _refresh_all(self) -> None:
        """后台线程一次抓取全部信息，一次刷新界面。"""
        if self.busy:
            return
        self._set_busy(True)
        self._set_status("刷新中…")
        Worker(self._queue, self._work_refresh).start()

    def _work_refresh(self) -> dict:
        """后台线程：收集文件树/变更/历史/分支数据。"""
        statuses = self.git.status()
        changed = {path: xy for xy, path in statuses}
        tree_items = self._build_tree_items()
        return {
            "action": "refresh",
            "statuses": statuses,
            "changed": changed,
            "tree": tree_items,
            "commits": self.git.commits(),
            "branches": self.git.branches(),
            "branch": self.git.current_branch(),
        }

    def _build_tree_items(self) -> list[tuple[str, str, str, str]]:
        """遍历工作目录，构造文件树节点 (父节点iid, iid, 显示名, 状态码)。

        目录 iid 用相对路径（如 "src"），文件 iid 用其相对路径；
        状态码用于着色：目录为空串，未变更文件为 "normal"。
        """
        items = []
        statuses = self.git.status()
        changed_map = {}
        for xy, path in statuses:
            changed_map.setdefault(path.replace("\\", "/"), xy)
        root = self.repo_dir
        if root is None:
            return items
        changed_dir = set()  # 含变更文件的目录，给目录也上色提示
        for path in changed_map:
            parts = path.split("/")[:-1]
            cur = ""
            for p in parts:
                cur = f"{cur}/{p}" if cur else p
                changed_dir.add(cur)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]  # 跳过 .git
            rel = Path(dirpath).relative_to(root).as_posix()
            parent = "" if rel == "." else rel
            for d in sorted(dirnames):
                iid = f"{rel}/{d}" if rel != "." else d
                tag = "dir" if iid not in changed_dir else "M"
                items.append((parent, iid, d, tag))
            for f in sorted(filenames):
                iid = f"{rel}/{f}" if rel != "." else f
                xy = changed_map.get(iid, "normal")
                items.append((parent, iid, f, xy))
        return items

    def _render(self, payload: dict) -> None:
        """用后台抓取的数据刷新各面板。"""
        # 文件树
        self.tree.delete(*self.tree.get_children())
        for parent, iid, name, tag in payload["tree"]:
            self.tree.insert(parent, "end", iid=iid, text=name,
                             open=bool(parent), tags=(tag,))
        # 变更列表
        self.status_tree.delete(*self.status_tree.get_children())
        for xy, path in payload["statuses"]:
            label, _ = STATUS_STYLE.get(xy, (xy, "#6b7280"))
            self.status_tree.insert("", "end", values=(label, path), tags=(xy,))
        # 提交历史
        self.log_tree.delete(*self.log_tree.get_children())
        for h, date, author, msg in payload["commits"]:
            self.log_tree.insert("", "end", values=(h, date, author, msg))
        # 分支
        self.branch_tree.delete(*self.branch_tree.get_children())
        for name, is_cur, msg in payload["branches"]:
            self.branch_tree.insert("", "end",
                                    values=("●" if is_cur else "", name, msg))
        # 状态栏
        remote = ""
        if self.git:
            remotes = self.git.remotes()
            remote = f"　远程：{remotes[0][1]}" if remotes else "　远程：(无)"
        n = len(payload["statuses"])
        self._set_status(f"已刷新：{n} 个变更{remote}")

    def _refresh_light(self) -> None:
        """操作后轻量刷新（内容与全量刷新一致，对常见仓库足够快）。"""
        if self.busy or self.git is None:
            return
        self._set_busy(True)
        Worker(self._queue, self._work_refresh).start()

    def _show_diff(self, payload: dict) -> None:
        """把差异内容显示到「差异」页签。"""
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        if payload["code"] != 0:
            self.diff_text.insert("end", payload["err"] or "无差异", "hdr")
        else:
            content = payload["out"] or "（无差异）"
            for line in content.splitlines():
                if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                    tag = "hdr"
                elif line.startswith("+"):
                    tag = "add"
                elif line.startswith("-"):
                    tag = "del"
                else:
                    tag = ""
                self.diff_text.insert("end", line + "\n", (tag,))
        self.diff_text.configure(state="disabled")
        self._set_status(f"差异：{payload['file']}")


# ---------------------------------------------------------------------------
# 提交对话框
# ---------------------------------------------------------------------------
class CommitDialog(tk.Toplevel):
    """提交对话框：填写提交信息，可选提交并推送。"""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("提交更改")
        self.transient(app)
        self.grab_set()
        self.geometry("560x340")

        ttk.Label(self, text="提交信息：").pack(anchor="w", padx=14, pady=(14, 2))
        self.msg_text = tk.Text(self, height=6, font=("Segoe UI", 10))
        self.msg_text.pack(fill="x", padx=14)
        self.msg_text.insert("1.0", "本次提交的说明…")
        self.msg_text.bind("<FocusIn>", self._clear_placeholder)

        ttk.Label(self, text="作者（可选，留空用 git 全局配置）：").pack(anchor="w", padx=14, pady=(8, 2))
        self.author_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.author_var).pack(fill="x", padx=14)

        self.push_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="提交并推送到远程（需要配置 Token 或已登录凭据）",
                        variable=self.push_var).pack(anchor="w", padx=14, pady=(4, 0))
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="提交前自动暂存全部改动（git add -A）",
                        variable=self.auto_var).pack(anchor="w", padx=14, pady=(2, 4))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=14, pady=12)
        ttk.Button(btns, text="提交", command=self._commit).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=4)

    def _clear_placeholder(self, _event=None) -> None:
        if self.msg_text.get("1.0", "end-1c").strip() == "本次提交的说明…":
            self.msg_text.delete("1.0", "end")

    def _commit(self) -> None:
        message = self.msg_text.get("1.0", "end-1c").strip()
        author = self.author_var.get().strip()
        if not message:
            messagebox.showwarning("提交更改", "请填写提交信息")
            return
        push = self.push_var.get()
        auto_add = self.auto_var.get()
        self.destroy()
        app = self.app
        app._set_busy(True)
        app._print("$ git add -A && git commit" + (" && git push" if push else ""), "cmd")

        def work(g: Git):
            return g.commit_and_push(message, author, push, auto_add)

        Worker(app._queue, app._work_console, work,
               "提交并推送" if push else "提交", False, "").start()


# ---------------------------------------------------------------------------
# 自测模式（无界面验证核心 git 操作）
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    """在临时目录里完整跑一遍核心 git 操作，验证软件所依赖的命令可靠。

    临时仓库建在脚本同目录下的 .selftest_repo（测试结束后删除），
    避免受系统临时目录权限影响。

    返回: 0 全部通过，1 有失败项。
    """
    tests: list[tuple[str, bool, str]] = []
    tmp = Path(__file__).parent / ".selftest_repo"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(exist_ok=True)

    def record(name: str, ok: bool, detail: str = "") -> None:
        tests.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))

    g = Git(repo_dir=tmp)
    try:
        # 1. 初始化
        code, _, err = g.run("init")
        record("git init", code == 0, err)
        # 2. 配置本地用户（自测仓库专用）
        g.run("config", "user.name", "Selftest")
        g.run("config", "user.email", "selftest@example.com")
        # 3. 新建文件并 add
        (tmp / "hello.txt").write_text("hello v1\n", encoding="utf-8")
        (tmp / "sub").mkdir()
        (tmp / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
        code, _, err = g.run("add", "-A")
        record("git add -A", code == 0, err)
        # 4. 状态解析
        status = g.status()
        record("status 解析", len(status) == 2, str(status))
        # 5. 首次提交
        code, _, err = g.run("commit", "-m", "first commit")
        record("git commit", code == 0, err)
        # 6. 历史解析
        commits = g.commits()
        record("log 解析", len(commits) == 1 and commits[0][3] == "first commit", str(commits))
        # 7. 当前分支
        branch = g.current_branch()
        record("当前分支", branch == "master" or branch == "main", branch)
        # 8. 修改文件、再提交第二个版本
        (tmp / "hello.txt").write_text("hello v2\n", encoding="utf-8")
        g.run("add", "-A")
        code, _, err = g.run("commit", "-m", "second commit")
        record("二次提交", code == 0, err)
        commits = g.commits()
        first_hash = commits[-1][0]  # 第一个提交（最新在最前）
        # 9. 分支创建与切换
        code, _, err = g.run("branch", "feature")
        code2, _, err2 = g.run("checkout", "feature")
        record("分支创建/切换", code == 0 and code2 == 0 and g.current_branch() == "feature",
               err or err2)
        # 10. 暂存与恢复
        (tmp / "hello.txt").write_text("hello v3\n", encoding="utf-8")
        code, _, err = g.run("stash", "push", "-u")
        record("stash 暂存", code == 0 and not g.status(), err)
        code, _, err = g.run("stash", "pop")
        record("stash 恢复", code == 0 and len(g.status()) == 1, err)
        # 11. 版本回溯：checkout 到旧版本新分支（git 要求工作区干净，先丢弃 v3 改动）
        g.run("checkout", "--", ".")
        code, _, err = g.run("checkout", "-b", "rollback-test", first_hash)
        record("回溯-检出新分支", code == 0 and g.current_branch() == "rollback-test", err)
        # 12. 版本回溯：soft reset 回第一个提交
        g.run("checkout", "master")
        code, _, err = g.run("reset", "--soft", first_hash)
        rec = g.commits()
        record("回溯-软回溯", code == 0 and len(rec) == 1, err)
        # 13. 版本回溯：hard reset 丢弃后续提交
        g.run("add", "-A")
        g.run("commit", "-m", "third commit")
        code, _, err = g.run("reset", "--hard", first_hash)
        rec = g.commits()
        record("回溯-硬回溯", code == 0 and len(rec) == 1 and rec[0][3] == "first commit",
               str(rec))
        # 14. 文件树遍历（与 _build_tree_items 相同的跳过 .git 逻辑）
        names = []
        for dirpath, dirnames, filenames in os.walk(tmp):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            names += filenames
        record("文件树遍历", "hello.txt" in names and "nested.txt" in names, str(names))
        # 15. 远程不存在时的 push 报错提示（不真连网）
        ok, msg = g.push()
        record("无远程 push 提示", not ok and "没有配置远程" in msg, msg)
        # 16. 结果归一化（防止 2 元素/3 元素返回混用导致后台任务解包崩溃）
        ok1 = normalize_git_result((True, "成功说明")) == (0, "成功说明", "")
        ok2 = normalize_git_result((False, "失败说明")) == (1, "", "失败说明")
        ok3 = normalize_git_result((7, "out", "err")) == (7, "out", "err")
        record("结果归一化", ok1 and ok2 and ok3, f"{ok1},{ok2},{ok3}")
        # 17. push 前置检查：分支没有提交时给出友好提示，而不是报 src refspec 错误
        tmp2 = Path(__file__).parent / ".selftest_push"
        if tmp2.exists():
            shutil.rmtree(tmp2, ignore_errors=True)
        tmp2.mkdir(exist_ok=True)
        g2 = Git(repo_dir=tmp2)
        g2.run("init", "-b", "main")
        g2.run("remote", "add", "origin", str(tmp2 / "x.git"))
        ok, msg = g2.push()
        record("push 无提交提示", not ok and "还没有任何提交" in msg, msg)
        shutil.rmtree(tmp2, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        record("异常", False, str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # 清理临时仓库

    failed = [t for t in tests if not t[1]]
    print(f"\n自测结果：{len(tests) - len(failed)}/{len(tests)} 通过")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    """命令行入口。

    用法:
        python git_manager.py               # 启动图形界面
        python git_manager.py <路径或网址>   # 启动并直接打开/克隆该仓库
        python git_manager.py --selftest    # 无界面自测核心 git 操作
    """
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    app = App()
    if len(sys.argv) > 1:
        app.addr_var.set(sys.argv[1])
        app.on_open_or_clone()
    app.mainloop()


if __name__ == "__main__":
    main()
