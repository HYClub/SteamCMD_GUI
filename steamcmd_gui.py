#!/usr/bin/env python3
"""SteamCMD GUI — 单文件版"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess, threading, queue, os, logging, time, traceback, re, json, base64
import ctypes, ctypes.wintypes
from pathlib import Path

__version__ = "20260626"
VERSION_URL = "https://raw.githubusercontent.com/HYClub/SteamCMD_GUI/master/steamcmd_gui.py"

# ── DPAPI 加密（仅 Windows）──────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

_P_CRYPTPROTECT = ctypes.windll.crypt32.CryptProtectData
_P_CRYPTPROTECT.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.wintypes.LPCWSTR,
    ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB)]
_P_CRYPTPROTECT.restype = ctypes.wintypes.BOOL

_P_CRYPTUNPROTECT = ctypes.windll.crypt32.CryptUnprotectData
_P_CRYPTUNPROTECT.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(ctypes.wintypes.LPCWSTR),
    ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB)]
_P_CRYPTUNPROTECT.restype = ctypes.wintypes.BOOL

def _dpapi_encrypt(plain: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(plain), ctypes.create_string_buffer(plain, len(plain)))
    blob_out = _DATA_BLOB()
    if not _P_CRYPTPROTECT(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 加密失败")
    data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return data

def _dpapi_decrypt(encrypted: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
    blob_out = _DATA_BLOB()
    if not _P_CRYPTUNPROTECT(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 解密失败")
    data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return data

def encrypt_pass(plain: str) -> str:
    if not plain: return ""
    return base64.b64encode(_dpapi_encrypt(plain.encode("utf-8"))).decode("ascii")

def decrypt_pass(encoded: str) -> str:
    if not encoded: return ""
    try:
        return _dpapi_decrypt(base64.b64decode(encoded)).decode("utf-8")
    except Exception:
        return encoded  # 兼容旧版明文

# ── 日志 ──────────────────────────────────
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07\x1b]*\x07|\x1b\[?\??[0-9;]*[a-zA-Z]|\x1b[()][AB012]')
LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / "steamcmd_gui.log"
if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1024 * 1024:
    LOG_FILE.write_text("")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("steamcmd_gui")

# ── 默认 App 列表 ─────────────────────────
DEFAULT_APPS = []

# ── 工具函数 ──────────────────────────────
def strip_ansi(text):
    return ANSI_RE.sub('', text)

def make_workshop_vdf(app_id,content_folder,preview_file,changenote,tags):
    import tempfile
    tags_f='""' if not tags else '"'+tags.replace(",",";")+'"'
    vdf='''"workshopitem"
{{
\t"appid"\t\t\t"{app_id}"
\t"contentfolder"\t"{content}"
\t"previewfile"\t"{preview}"
\t"changenote"\t\t"{note}"
\t"tags"\t\t\t{tags}
}}'''.format(app_id=app_id, content=content_folder.replace("\\","/"),
             preview=(preview_file or "").replace("\\","/"), note=changenote or "", tags=tags_f)
    fd,path=tempfile.mkstemp(suffix=".vdf",prefix="steamcmd_upload_",text=True)
    with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(vdf)
    return path

def make_app_build_vdf(app_id, desc, content_root, build_output, depots):
    depot_lines = ""
    for d in depots:
        depot_lines += f'\t\t"{d}"\t\t"depot_build_{d}.vdf"\n'
    return (f'"AppBuild"\n'
            '{\n'
            f'\t"AppID"\t\t"{app_id}"\n'
            f'\t"Desc"\t\t"{desc}"\n'
            f'\t"ContentRoot"\t"{content_root}"\n'
            f'\t"BuildOutput"\t"{build_output}"\n'
            '\n'
            '\t"Depots"\n'
            '\t{\n'
            f'{depot_lines}'
            '\t}\n'
            '}')

def make_depot_build_vdf(depot_id):
    return ('"DepotBuild"\n'
            '{\n'
            f'\t"DepotID"\t"{depot_id}"\n'
            '\n'
            '\t"FileMapping"\n'
            '\t{\n'
            '\t\t"LocalPath"\t\t"*"\n'
            '\t\t"DepotPath"\t\t"."\n'
            '\t\t"Recursive"\t\t"1"\n'
            '\t}\n'
            '\n'
            '\t"FileExclusion"\t"*.pdb"\n'
            '\t"FileExclusion"\t"*.log"\n'
            '}')

# ── 配置 ──────────────────────────────────
class Config:
    def __init__(self):
        self.path = Path(__file__).with_suffix(".cfg")
        self.data = self._load()
    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            try:
                data = json.loads(base64.b64decode(raw).decode("utf-8"))
                is_b64 = True
            except Exception:
                data = json.loads(raw)
                is_b64 = False
            changed = False
            for l in data.get("logins", []):
                for key in ("user","pass"):
                    v = l.get(key, "")
                    if not v: continue
                    try:
                        _dpapi_decrypt(base64.b64decode(v))
                    except Exception:
                        try:
                            base64.b64decode(v)
                            l["valid"] = False
                        except:
                            l[key] = encrypt_pass(v)
                            changed = True
            if changed or not is_b64:
                self.data = data
                self.save()
            return data
        except:
            return {"logins":[],"apps":DEFAULT_APPS}
    def save(self):
        for k in ("steam_path","def_dir","recent"):
            self.data.pop(k, None)
        txt = base64.b64encode(json.dumps(self.data,indent=2,ensure_ascii=False).encode("utf-8")).decode("ascii")
        self.path.write_text(txt, encoding="utf-8")
    def reload(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            try:
                self.data = json.loads(base64.b64decode(raw).decode("utf-8"))
            except Exception:
                self.data = json.loads(raw)
        except: pass
    @property
    def logins(self): return self.data.get("logins",[])
    @property
    def apps(self): return self.data.get("apps",DEFAULT_APPS)
    @apps.setter
    def apps(self,v): self.data["apps"]=v; self.save()
    def visible_apps(self): return [(a["id"],a["name"]) for a in self.apps if a.get("show",True)]
    def add_login(self,u,p):
        ls = self.logins
        ls = [l for l in ls if decrypt_pass(l.get("user","")) != u]
        ls.insert(0, {"user": encrypt_pass(u), "pass": encrypt_pass(p)})
        self.data["logins"]=ls[:10]; self.save()
    def del_login(self, idx):
        ls = self.logins
        if idx < len(ls):
            ls.pop(idx)
            self.data["logins"] = ls
            self.save()

# ── ConPTY 封装 ──────────────────────────
for _ in range(2):
    try:
        import winpty
        break
    except ImportError:
        import subprocess, sys, os
        try:
            si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; si.wShowWindow = subprocess.SW_HIDE
            subprocess.run([sys.executable, "-m", "pip", "install", "pywinpty", "-q"],
                timeout=60, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True)
        except Exception:
            pass
else:
    import tkinter.messagebox as mb
    mb.showerror("错误", "缺少 pywinpty，自动安装失败，请手动运行:\n\npip install pywinpty")
    raise SystemExit(1)

class ConPTY:
    def __init__(self, cols=120, rows=30):
        self.cols = cols; self.rows = rows; self._pty = None

    def start(self, exe_path, cwd=None, extra_args=None):
        if extra_args:
            cmdline = exe_path + " " + " ".join(extra_args)
        else:
            cmdline = None
        log.info("[ConPTY] 启动 cmdline=%s cwd=%s", cmdline or exe_path, cwd)
        try:
            self._pty = winpty.PTY(cols=self.cols, rows=self.rows)
            self._pty.spawn(exe_path, cmdline=cmdline, cwd=cwd)
            log.info("[ConPTY] 进程启动成功, PID=%s", self._pty.pid)
            return 0
        except Exception as e:
            log.error("[ConPTY] 启动失败: %s", e); return -1

    def write(self, data):
        if not self._pty: return False
        try:
            text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            self._pty.write(text); return True
        except Exception as e:
            log.error("[ConPTY] write 异常: %s", e); return False

    def read(self, blocking=False):
        if not self._pty: return b""
        try:
            text = self._pty.read(blocking=blocking)
            return text.encode("utf-8", errors="replace") if isinstance(text, str) else text if text else b""
        except: return b""

    @property
    def pid(self): return self._pty.pid if self._pty else None

    def is_alive(self):
        if not self._pty: return False
        try: return self._pty.isalive()
        except: return False

    def close(self):
        if self._pty:
            try: self._pty.close()
            except: pass
            self._pty = None

    def terminate(self):
        if self._pty and self.pid:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x0001, False, self.pid)
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 1)
                ctypes.windll.kernel32.CloseHandle(h)
        self.close()

# ── SteamCMD 进程管理 ────────────────────
class SteamCMD:
    def __init__(self, path="steamcmd.exe"):
        self.path = path; self._conpty = None
        self._buf = queue.Queue(); self._reader_stop = threading.Event()
        self.on_output = None; self._partial = b""
        self._lock = threading.Lock(); self._first_push = threading.Event()

    def start(self, extra_args=None):
        self._buf = queue.Queue(); self._reader_stop.clear()
        self._partial = b""; self._first_push.clear()
        abs_path = os.path.abspath(self.path)
        cwd = os.path.dirname(abs_path)
        cpty = ConPTY(cols=120, rows=30)
        args = [abs_path] + (extra_args or [])
        hr = cpty.start(args[0], extra_args=args[1:], cwd=cwd)
        if hr != 0:
            if self.on_output: self.on_output("ConPTY 启动失败")
            return False
        self._conpty = cpty
        self._start_conpty_reader()
        return True

    def _start_conpty_reader(self):
        def _reader():
            while not self._reader_stop.is_set():
                chunk = self._conpty.read(blocking=False)
                if not chunk:
                    if not self._conpty.is_alive():
                        with self._lock:
                            leftover = self._partial; self._partial = b""
                        if leftover: self._push(leftover)
                        self._reader_stop.set(); self._first_push.set(); return
                    time.sleep(0.05); continue
                with self._lock:
                    self._partial += chunk
                    lines = []
                    while b"\n" in self._partial:
                        l, self._partial = self._partial.split(b"\n", 1)
                        lines.append(l.rstrip(b"\r"))
                for l in lines: self._push(l)

        threading.Thread(target=_reader, daemon=True, name="conpty-reader").start()

        def _flusher():
            while not self._reader_stop.is_set():
                time.sleep(1)
                with self._lock:
                    if self._partial:
                        data = self._partial; self._partial = b""
                    else: data = b""
                if data: self._push(data)

        threading.Thread(target=_flusher, daemon=True, name="conpty-flusher").start()

    _SENSITIVE_RE = None

    def _mask(self, text):
        if SteamCMD._SENSITIVE_RE is None:
            SteamCMD._SENSITIVE_RE = [
                re.compile(r"(login\s+\S+\s+)(\S+)", re.IGNORECASE),
                re.compile(r"(set_steam_guard_code\s+)(\S+)", re.IGNORECASE),
            ]
        for p in SteamCMD._SENSITIVE_RE:
            m = p.search(text)
            if m: text = text[:m.start(2)] + "***" + text[m.end(2):]
        return text

    def _push(self, data):
        if not data: return
        text = strip_ansi(data.decode("utf-8", errors="replace")).replace("\r", "")
        if not text.strip(): return
        masked = self._mask(text)
        log.debug("[push] %s", masked)
        self._buf.put(masked); self._first_push.set()
        if self.on_output:
            try: self.on_output(masked)
            except Exception as e: log.error("[push] on_output 回调异常: %s", e)

    def send(self, cmd):
        if not self._conpty or not self._conpty.is_alive(): return False
        try:
            raw = (cmd + "\n").encode("utf-8")
            safe = re.sub(r'(login\s+\S+\s+)(\S+)', r'\1***', cmd, flags=re.IGNORECASE)
            safe = re.sub(r'(set_steam_guard_code\s+)(\S+)', r'\1***', safe, flags=re.IGNORECASE)
            log.info("[send] >>> %s (raw=%d bytes)", safe, len(raw))
            return self._conpty.write(raw)
        except Exception as e: log.error("[send] 失败: %s", e); return False

    def read_until(self, targets, timeout=10):
        lines = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                partial_text = self._partial.decode("utf-8", errors="replace")
            for t in targets:
                if t.lower() in partial_text.lower(): return lines, t
            try:
                line = self._buf.get(timeout=0.15); lines.append(line)
                for t in targets:
                    if t.lower() in line.lower(): return lines, t
            except queue.Empty: pass
        return lines, None

    def is_alive(self):
        if self._conpty: return self._conpty.is_alive()
        return False

    def stop(self):
        self._reader_stop.set()
        if self._conpty: self._conpty.terminate()

    @staticmethod
    def find_path():
        for p in ["steamcmd.exe", os.path.join("..","steamcmd.exe")]:
            if os.path.isfile(p): return os.path.abspath(p)
        return None

# ── GUI ──────────────────────────────────
class App:
    def __init__(self):
        self.cfg = Config(); self.steamcmd = None
        self.running = False
        self._update_available = None
        self.root = tk.Tk()
        self.root.title("SteamCMD GUI"); self.root.geometry("750x580")
        self.root.minsize(700, 500)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._startup)
        self.root.after(5000, self._check_update_startup)

    def _build_ui(self):
        main = ttk.Frame(self.root); main.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self._update_bar = tk.Frame(main, bg="#fff3cd", relief=tk.FLAT, bd=0)
        self._update_btn = tk.Button(self._update_bar, text="📥 新版本可用", bg="#fff3cd",
            fg="#856404", relief=tk.FLAT, activebackground="#ffe69c",
            font=("微软雅黑",9), cursor="hand2", bd=0,
            command=self._show_update_dialog)
        self._update_btn.pack(side=tk.LEFT, padx=10, pady=2)
        self._update_bar.pack_forget()

        self._console_frame = ttk.LabelFrame(main, text="控制台输出")
        self._console_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self._console_frame.configure(height=150); self._console_frame.pack_propagate(False)
        self.console = tk.Text(self._console_frame, height=6, font=("Consolas",9),
            state=tk.DISABLED, bg="#0d1117", fg="#c9d1d9", relief=tk.SUNKEN, bd=1)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2,0), pady=2)
        scb = ttk.Scrollbar(self._console_frame, command=self.console.yview)
        self.console.configure(yscrollcommand=scb.set)
        scb.pack(side=tk.RIGHT, fill=tk.Y, pady=2)

        top_frame = ttk.Frame(main); top_frame.pack(fill=tk.BOTH, expand=True)
        self.nb = ttk.Notebook(top_frame); self.nb.pack(fill=tk.BOTH, expand=True)
        self.tab_home = ttk.Frame(self.nb); self.tab_upload = ttk.Frame(self.nb)
        self.tab_login = ttk.Frame(self.nb); self.tab_settings = ttk.Frame(self.nb)
        self.nb.add(self.tab_home, text="主页"); self.nb.add(self.tab_login, text="登录", state="disabled")
        self.nb.add(self.tab_upload, text="上传", state="disabled"); self.nb.add(self.tab_settings, text="设置")

        self._build_home(); self._build_upload(); self._build_login(); self._build_settings()

        status_frame = tk.Frame(self.root, relief=tk.SUNKEN, bd=2); status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status = tk.Label(status_frame, text="就绪", anchor=tk.W, padx=5)
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.login_indicator = tk.Label(status_frame, text="未登录", fg="#888",
            anchor=tk.E, padx=10, font=("微软雅黑",9))
        self.login_indicator.pack(side=tk.RIGHT)

    def _on_close(self):
        if self.running and not messagebox.askyesno("确认", "正在上传中，确定要关闭吗？", parent=self.root):
            return
        if self.steamcmd:
            self.console_append("正在关闭 SteamCMD...")
            self.steamcmd.stop()
        self.root.destroy()

    def _startup(self):
        self.console_append("=" * 40); self.console_append("定位 SteamCMD...")
        path = SteamCMD.find_path()
        if not path:
            self.console_append("未找到 steamcmd.exe"); self.status.configure(text="未找到 SteamCMD")
            messagebox.showwarning("提示", "请在 SteamCMD 目录下运行本程序\n\n将 steamcmd_gui.py 放到 steamcmd.exe 同目录即可")
            return
        self.console_append(path)
        self.console_append("正在启动 SteamCMD 自更新...")
        self.status.configure(text="SteamCMD 更新中...")
        sc = SteamCMD(path)
        sc.on_output = lambda line: self.root.after(0, self.console_append, line)
        sc.start(extra_args=["+quit"])
        threading.Thread(target=self._wait_startup, args=(sc,), daemon=True).start()

    def _wait_startup(self, sc):
        if sc._reader_stop.wait(timeout=60):
            self.root.after(0, lambda: self.console_append("初始化完成 — 请登录 Steam 账号"))
        else:
            self.root.after(0, lambda: self.console_append("SteamCMD 自更新超时，仍可尝试登录"))
            sc.stop()
        self.root.after(0, lambda: self.home_btns[0].configure(state=tk.NORMAL))
        self.root.after(0, lambda: self.nb.tab(self.tab_login, state="normal"))
        self.root.after(0, lambda: self.status.configure(text="SteamCMD 就绪"))

    def console_append(self, text):
        self.console.configure(state=tk.NORMAL)
        self.console.insert(tk.END, text + "\n"); self.console.see(tk.END)
        self.console.configure(state=tk.DISABLED)

    def _center(self, w, width, height):
        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - height) // 2
        w.geometry(f"{width}x{height}+{x}+{y}")

    def _browse_file(self, e, t=None):
        if t is None: t=[("所有文件","*.*")]
        f = filedialog.askopenfilename(title="选择文件", filetypes=t)
        if f: e.delete(0, tk.END); e.insert(0, f)

    def _browse_dir(self, e):
        d = filedialog.askdirectory(title="选择目录")
        if d: e.delete(0, tk.END); e.insert(0, d)

    # ── 主页 ──────────────────────────────
    def _build_home(self):
        f = self.tab_home
        tk.Label(f, text="SteamCMD GUI", font=("微软雅黑",18,"bold"), fg="#00aaff").pack(pady=(20,5))
        bf = ttk.Frame(f); bf.pack(pady=20)
        self.home_btns = []
        for text,idx in [("登录",1),("上传",2),("设置",3)]:
            btn = tk.Button(bf, text=text, font=("微软雅黑",12),
                command=lambda i=idx: self.nb.select(i), width=20, height=2,
                relief=tk.GROOVE, bd=2)
            btn.pack(pady=5)
            self.home_btns.append(btn)
        self.home_btns[0].configure(state=tk.DISABLED)
        self.home_btns[1].configure(state=tk.DISABLED)

    # ── 上传 ──────────────────────────────
    def _build_upload(self):
        f = self.tab_upload
        self.ul_warn = tk.Label(f, text="⚠ 需要先登录 Steam 账号", fg="#ee4444", font=("微软雅黑",9))
        self.ul_warn.pack(pady=(10,2))

        self.ul_mode = tk.StringVar(value="workshop")
        mode_frame = ttk.Frame(f); mode_frame.pack(pady=(0,5))
        ttk.Radiobutton(mode_frame, text="Workshop 工坊", variable=self.ul_mode, value="workshop",
                        command=self._toggle_ul_mode).pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(mode_frame, text="SteamPipe 正式版", variable=self.ul_mode, value="steampipe",
                        command=self._toggle_ul_mode).pack(side=tk.LEFT, padx=10)

        self.ul_frame = ttk.LabelFrame(f, text="上传配置"); self.ul_frame.pack(fill=tk.X, padx=10, pady=5)

        # App ID (shared for both modes)
        self.ul_appid_frame = ttk.Frame(self.ul_frame)
        ttk.Label(self.ul_appid_frame, text="游戏 App ID:", width=12).pack(side=tk.LEFT)
        self.ul_appid = ttk.Entry(self.ul_appid_frame, width=15); self.ul_appid.pack(side=tk.LEFT, padx=5)
        ttk.Button(self.ul_appid_frame, text="常用游戏", command=self._pick_ul_game).pack(side=tk.LEFT, padx=5)

        # Content folder (shared for both modes)
        self.ul_content_frame = ttk.Frame(self.ul_frame)
        ttk.Label(self.ul_content_frame, text="内容文件夹:", width=12).pack(side=tk.LEFT)
        self.ul_content = ttk.Entry(self.ul_content_frame)
        self.ul_content.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(self.ul_content_frame, text="浏览", command=lambda: (self._browse_dir(self.ul_content), self._check_ul_ready())).pack(side=tk.LEFT)

        # Workshop 专用
        self.ul_ws_frame = ttk.Frame(self.ul_frame)
        r3 = ttk.Frame(self.ul_ws_frame); r3.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(r3, text="预览图片:", width=12).pack(side=tk.LEFT)
        self.ul_preview = ttk.Entry(r3); self.ul_preview.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(r3, text="浏览", command=lambda: self._browse_file(self.ul_preview, [("图片","*.jpg;*.png")])).pack(side=tk.LEFT)

        r4 = ttk.Frame(self.ul_ws_frame); r4.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(r4, text="标签(逗号分隔):", width=12).pack(side=tk.LEFT)
        self.ul_tags = ttk.Entry(r4); self.ul_tags.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        r5 = ttk.LabelFrame(self.ul_ws_frame, text="更新说明"); r5.pack(fill=tk.X, padx=5, pady=3)
        self.ul_note = tk.Text(r5, height=3, font=("Consolas",9), wrap=tk.WORD)
        self.ul_note.pack(fill=tk.X, padx=5, pady=3)

        # SteamPipe 专用
        self.ul_sp_frame = ttk.Frame(self.ul_frame)
        sp1 = ttk.Frame(self.ul_sp_frame); sp1.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(sp1, text="Depot ID:", width=12).pack(side=tk.LEFT)
        self.ul_depot = ttk.Combobox(sp1, state="readonly", width=15); self.ul_depot.pack(side=tk.LEFT, padx=5)
        ttk.Button(sp1, text="📡 查询", command=self._fetch_depots).pack(side=tk.LEFT, padx=2)
        self.ul_depot.bind("<<ComboboxSelected>>", lambda e: self._check_ul_ready())

        sp2 = ttk.Frame(self.ul_sp_frame); sp2.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(sp2, text="版本描述:", width=12).pack(side=tk.LEFT)
        self.ul_desc = ttk.Entry(sp2); self.ul_desc.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ul_desc.insert(0, "v0.1.0 first upload")

        sp3 = ttk.Frame(self.ul_sp_frame); sp3.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(sp3, text="输出目录:", width=12).pack(side=tk.LEFT)
        base = Path(__file__).parent / "output"
        self.ul_build_out = ttk.Entry(sp3); self.ul_build_out.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ul_build_out.insert(0, str(base))
        ttk.Button(sp3, text="浏览", command=lambda: (self._browse_dir(self.ul_build_out), self._check_ul_ready())).pack(side=tk.LEFT)

        self.ul_btn = ttk.Button(f, text="📤 开始上传", command=self._start_upload)
        self.ul_btn.pack(pady=8)

        self.ul_progress = ttk.Progressbar(f, mode='determinate', length=400)
        self.ul_progress_label = tk.Label(f, text="", fg="#888", font=("微软雅黑",9))

        # 默认 Workshop 布局
        self.ul_appid_frame.pack(fill=tk.X, padx=5, pady=3)
        self.ul_content_frame.pack(fill=tk.X, padx=5, pady=3)
        self.ul_ws_frame.pack(fill=tk.X)
        self.ul_sp_frame.pack_forget()
        self.ul_btn.configure(state=tk.DISABLED)

        self.ul_appid.bind("<KeyRelease>", lambda e: self._check_ul_ready())
        self.ul_content.bind("<KeyRelease>", lambda e: self._check_ul_ready())
        self.ul_desc.bind("<KeyRelease>", lambda e: self._check_ul_ready())
        self.ul_build_out.bind("<KeyRelease>", lambda e: self._check_ul_ready())

    def _check_ul_ready(self):
        if self.ul_mode.get() == "workshop":
            ok = bool(self.ul_appid.get().strip() and self.ul_content.get().strip())
        else:
            ok = bool(self.ul_appid.get().strip() and self.ul_depot.get().strip()
                      and self.ul_content.get().strip() and self.ul_desc.get().strip()
                      and self.ul_build_out.get().strip())
        self.ul_btn.configure(state=tk.NORMAL if ok else tk.DISABLED)

    def _toggle_ul_mode(self):
        for w in (self.ul_appid_frame, self.ul_content_frame, self.ul_ws_frame, self.ul_sp_frame):
            w.pack_forget()
        self.ul_appid_frame.pack(fill=tk.X, padx=5, pady=3)
        if self.ul_mode.get() == "workshop":
            self.ul_content_frame.pack(fill=tk.X, padx=5, pady=3)
            self.ul_ws_frame.pack(fill=tk.X)
            self.ul_frame.configure(text="上传配置")
        else:
            self.ul_sp_frame.pack(fill=tk.X)
            self.ul_content_frame.pack(fill=tk.X, padx=5, pady=3)
            self.ul_frame.configure(text="SteamPipe 构建配置")
        self._check_ul_ready()

    def _pick_ul_game(self):
        self.cfg.reload()
        apps = self.cfg.visible_apps()
        if not apps: messagebox.showinfo("提示", "请到设置页添加"); return
        d = tk.Toplevel(self.root); d.title("选择游戏")
        self._center(d, 350, 300)
        d.transient(self.root); d.grab_set()
        lb = tk.Listbox(d, font=("Consolas",10)); lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for id_, name in apps: lb.insert(tk.END, "[{}] {}".format(id_, name))
        def sel():
            i = lb.curselection()
            if i and i[0] < len(apps): self.ul_appid.delete(0, tk.END); self.ul_appid.insert(0, apps[i[0]][0])
            d.destroy(); self._check_ul_ready()
        ttk.Button(d, text="确定", command=sel).pack(pady=5)

    def _start_upload(self):
        if self.running: return
        app_id = self.ul_appid.get().strip()
        content = self.ul_content.get().strip()
        if not app_id or not content: messagebox.showwarning("提示", "请填写 App ID 和内容文件夹"); return
        if not os.path.isdir(content): messagebox.showwarning("提示", "内容文件夹不存在"); return
        if not self.steamcmd or not self.steamcmd.is_alive():
            messagebox.showwarning("提示", "SteamCMD 未运行，请先登录"); return

        if self.ul_mode.get() == "workshop":
            self._start_ws_upload(app_id, content)
        else:
            self._start_sp_upload(app_id, content)

    def _on_steamcmd_output(self, line):
        self.root.after(0, self.console_append, line)
        clean = line.replace("\r", "")
        pcts = re.findall(r'Uploading.*\((\d+(?:\.\d+)?)%\)', clean)
        if pcts:
            pct = float(pcts[-1])
            self.root.after(0, lambda p=pct: self.ul_progress.configure(value=p, mode='determinate'))
            self.root.after(0, lambda p=pct: self.ul_progress_label.configure(text=f"上传中 {p:.0f}%"))
        elif 'Build ID' in clean:
            self.root.after(0, lambda: self.ul_progress.configure(value=100))
            self.root.after(0, lambda: self.ul_progress_label.configure(text="完成"))
        elif 'Scanning content' in clean or 'Building file mapping' in clean or 'Building depot' in clean:
            self.root.after(0, lambda: self.ul_progress.configure(mode='indeterminate'))
            self.root.after(0, lambda: self.ul_progress.start(10))
            self.root.after(0, lambda: self.ul_progress_label.configure(text="准备中..."))

    def _reset_progress(self):
        self.ul_progress.stop()
        self.ul_progress.pack_forget()
        self.ul_progress_label.pack_forget()
        self.ul_progress.configure(value=0, mode='determinate')

    def _fetch_depots(self):
        app_id = self.ul_appid.get().strip()
        if not app_id:
            messagebox.showwarning("提示", "请先填写 App ID", parent=self.root); return
        if not self.steamcmd or not self.steamcmd.is_alive():
            messagebox.showwarning("提示", "SteamCMD 未登录", parent=self.root); return
        self.console_append(f"> 正在查询 App {app_id} 的 Depot 列表...")
        with self.steamcmd._lock:
            self.steamcmd._partial = b""
        while not self.steamcmd._buf.empty():
            try: self.steamcmd._buf.get_nowait()
            except: break
        self.steamcmd.send(f"app_info_print {app_id}")
        lines, matched = self.steamcmd.read_until(["steam>"], timeout=30)
        if not matched:
            self.console_append("⏱ 查询超时"); return
        depots = set()
        stack = []
        for line in lines:
            s = line.strip()
            if s == '}':
                if stack: stack.pop()
            elif s == '{':
                continue
            else:
                m = re.match(r'^"([^"]+)"$', s)
                if m:
                    key = m.group(1)
                    if len(stack) >= 1 and stack[-1] == "depots" and key.isdigit():
                        depots.add(key)
                    stack.append(key)
        if not depots:
            self.console_append("未找到 Depot"); return
        self.console_append(f"找到 {len(depots)} 个 Depot: {', '.join(sorted(depots))}")
        sorted_depots = sorted(depots)
        self.ul_depot["values"] = sorted_depots
        self.ul_depot.set(sorted_depots[0])
        self._check_ul_ready()

    def _start_ws_upload(self, app_id, content):
        vdf = make_workshop_vdf(app_id, content, self.ul_preview.get().strip(),
                                self.ul_note.get("1.0", tk.END).strip(), self.ul_tags.get().strip())
        self.console_append("VDF 已创建: " + vdf)
        self.console_append("> workshop_build_item " + vdf)
        self.running = True
        self.ul_progress.pack(pady=(0,5))
        self.ul_progress_label.pack()
        self.ul_progress.configure(value=0, mode='indeterminate')
        self.ul_progress.start(10)
        self.ul_progress_label.configure(text="准备中...")
        sc = self.steamcmd
        sc.send("workshop_build_item \"" + vdf + "\"")
        threading.Thread(target=self._upload_wait, args=(sc,), daemon=True).start()

    def _start_sp_upload(self, app_id, content):
        depot = self.ul_depot.get().strip()
        desc = self.ul_desc.get().strip()
        build_out = self.ul_build_out.get().strip()
        if not depot or not desc or not build_out:
            messagebox.showwarning("提示", "请填写 Depot ID / 版本描述 / 输出目录"); return
        scripts_dir = Path(__file__).parent / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        Path(build_out).mkdir(parents=True, exist_ok=True)

        app_vdf = scripts_dir / f"app_build_{app_id}.vdf"
        depot_vdf = scripts_dir / f"depot_build_{depot}.vdf"
        content_root = os.path.abspath(content)

        depot_vdf.write_text(make_depot_build_vdf(depot), encoding="utf-8")
        app_vdf.write_text(make_app_build_vdf(app_id, desc, content_root,
                                              os.path.abspath(build_out), [depot]), encoding="utf-8")
        self.console_append(f"脚本已生成: {app_vdf}")
        self.console_append(f"脚本已生成: {depot_vdf}")
        self.console_append("> run_app_build " + str(app_vdf))
        self.running = True
        self.ul_progress.pack(pady=(0,5))
        self.ul_progress_label.pack()
        self.ul_progress.configure(value=0, mode='indeterminate')
        self.ul_progress.start(10)
        self.ul_progress_label.configure(text="准备中...")
        sc = self.steamcmd
        sc.send(f"run_app_build \"{app_vdf}\"")
        threading.Thread(target=self._upload_wait, args=(sc,), daemon=True).start()

    def _upload_wait(self, sc):
        try:
            lines, matched = sc.read_until(["FAILED", "ERROR", "Success", "complete", "BuildID"], timeout=600)
            log.info("[UPLOAD] 结果: matched=%s, lines=%d", matched, len(lines))
            if matched and ("FAILED" in matched or "ERROR" in matched):
                self.root.after(0, lambda m=matched: self.console_append("❌ 上传失败: " + m))
                self.root.after(0, lambda: messagebox.showerror("上传失败", matched, parent=self.root))
            elif matched and "BuildID" in matched:
                self.root.after(0, lambda: self.console_append("✅ 上传完成 — 请到 Steamworks 后台设置 Live"))
                self.root.after(0, lambda: messagebox.showinfo("上传成功", "请在 Steamworks 后台设置 Live", parent=self.root))
            elif matched:
                self.root.after(0, lambda: self.console_append("✅ 上传完成"))
                self.root.after(0, lambda: messagebox.showinfo("上传成功", "", parent=self.root))
            else:
                self.root.after(0, lambda: self.console_append("⏱ 超时，上传可能已完成"))
                self.root.after(0, lambda: messagebox.showwarning("上传超时", "上传可能已完成，请检查 Steamworks", parent=self.root))
        except Exception as e:
            log.error("[UPLOAD] 异常: %s", e)
            self.root.after(0, lambda: self.console_append("错误: " + str(e)))
        finally:
            self.running = False
            self.root.after(0, self._reset_progress)

    # ── 登录 ──────────────────────────────
    def _build_login(self):
        f = self.tab_login
        self.login_status = tk.StringVar(value="输入账号密码后点击登录")
        tk.Label(f, textvariable=self.login_status, font=("微软雅黑",10), fg="#888").pack(pady=(10,0))

        card = ttk.LabelFrame(f, text="登录"); card.pack(fill=tk.X, padx=10, pady=5)
        self.login_card = card

        # ── 表单内容 ──
        self.login_form_frame = ttk.Frame(card)
        r1 = ttk.Frame(self.login_form_frame); r1.pack(fill=tk.X, padx=15, pady=5)
        tk.Label(r1, text="Steam 用户名:", width=14, anchor=tk.E).pack(side=tk.LEFT)
        self.login_user = ttk.Entry(r1, font=("微软雅黑",11))
        self.login_user.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.login_user.bind("<Return>", lambda e: self.login_pass.focus())
        self.login_user.focus()

        r2 = ttk.Frame(self.login_form_frame); r2.pack(fill=tk.X, padx=15, pady=5)
        tk.Label(r2, text="密码:", width=14, anchor=tk.E).pack(side=tk.LEFT)
        self.login_pass = ttk.Entry(r2, show="*", font=("微软雅黑",11))
        self.login_pass.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.login_pass.bind("<Return>", lambda e: self._login_start())

        self.login_guard_frame = ttk.Frame(self.login_form_frame)
        rg = ttk.Frame(self.login_guard_frame); rg.pack(fill=tk.X, padx=15, pady=5)
        tk.Label(rg, text="Steam Guard 验证码:", width=14, anchor=tk.E, fg="#ee4444").pack(side=tk.LEFT)
        self.login_guard = ttk.Entry(rg, font=("Consolas",16), width=12)
        self.login_guard.pack(side=tk.LEFT, padx=5)
        self.login_guard.bind("<Return>", lambda e: self._login_submit_guard())
        self.login_guard_btn = ttk.Button(rg, text="确认验证码", command=self._login_submit_guard)
        self.login_guard_btn.pack(side=tk.LEFT, padx=5)

        ba = ttk.Frame(self.login_form_frame); ba.pack(fill=tk.X, padx=15, pady=8)
        self.login_btn = ttk.Button(ba, text="开始登录", command=self._login_start, width=15)
        self.login_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(ba, text="保存账号", command=self._save_login).pack(side=tk.LEFT, padx=5)
        self.login_form_frame.pack(fill=tk.X)

        # ── 已登录视图（同位置）──
        self.login_loggedin_frame = ttk.Frame(card)
        info = ttk.Frame(self.login_loggedin_frame); info.pack(fill=tk.X, padx=15, pady=10)
        tk.Label(info, text="用户名:", width=10, anchor=tk.E).grid(row=0, column=0, sticky=tk.E, pady=3)
        self.login_loggedin_user = tk.Label(info, font=("微软雅黑",11), fg="#333", anchor=tk.W)
        self.login_loggedin_user.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)
        tk.Label(info, text="状态:", width=10, anchor=tk.E).grid(row=1, column=0, sticky=tk.E, pady=3)
        tk.Label(info, text="✅ 已登录", font=("微软雅黑",10), fg="#00cc66", anchor=tk.W).grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)
        ba2 = ttk.Frame(self.login_loggedin_frame); ba2.pack(fill=tk.X, padx=15, pady=8)
        ttk.Button(ba2, text="退出登录", command=self._logout).pack(side=tk.LEFT, padx=5)
        ttk.Button(ba2, text="保存用户", command=self._save_login).pack(side=tk.LEFT, padx=5)

        sf = ttk.LabelFrame(f, text="已保存账号（双击自动登录）")
        sf.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.login_list = tk.Listbox(sf, height=3, font=("Consolas",10))
        self.login_list.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.login_list.bind("<Double-Button-1>", self._login_from_saved)
        self._refresh_logins()
        bf = ttk.Frame(f); bf.pack(pady=5)
        ttk.Button(bf, text="🗑 删除选中", command=self._delete_login).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="🧹 清除缓存", command=self._clear_creds).pack(side=tk.LEFT, padx=5)

    def _start_steamcmd_with_args(self, extra_args):
        path = SteamCMD.find_path()
        if not path: return None
        if self.steamcmd: self.steamcmd.stop(); self.steamcmd = None
        sc = SteamCMD(path)
        sc.on_output = self._on_steamcmd_output
        sc.start(extra_args=extra_args)
        self.steamcmd = sc; return sc

    def _login_start(self):
        u = self.login_user.get().strip(); pw = self.login_pass.get()
        if not u or not pw: return
        self.login_user_val = u
        self.console_append("> (正在登录...)")
        self.login_status.set("正在登录，请耐心等待...")
        self.login_btn.configure(state=tk.DISABLED, text="登录中...")
        self._start_steamcmd_with_args(["+@NoPromptForPassword", "1", "+login", u, pw])
        threading.Thread(target=self._login_wait_guard, daemon=True).start()

    def _login_wait_guard(self):
        try:
            log.info("[LOGIN] _login_wait_guard 开始, 进程存活: %s", self.steamcmd.is_alive())
            lines, matched = self.steamcmd.read_until(["set_steam_guard_code","Invalid Password","Waiting for user info","Login Failure","Login failed","Account Logon Denied"], timeout=90)
            log.info("[LOGIN] _login_wait_guard 结果: matched=%s, lines=%d", matched, len(lines))
            if matched and ("set_steam_guard_code" in matched or "Account Logon Denied" in matched): self.root.after(0, self._login_show_guard)
            elif matched and "Invalid" in matched: self.root.after(0, lambda: self._login_reset("密码错误"))
            elif matched and "Waiting" in matched: self.root.after(0, self._login_done)
            elif matched and ("Login" in matched or "fail" in matched.lower()): self.root.after(0, lambda: self._login_reset("登录失败"))
            else: self.root.after(0, lambda: self._login_reset("超时或无响应"))
        except Exception as e:
            log.error("[LOGIN] _login_wait_guard 异常: %s", e)
            self.root.after(0, lambda: self.console_append("错误: " + str(e)))

    def _login_show_guard(self):
        self.login_status.set("需要 Steam Guard 验证码，请输入后点击确认")
        self.login_guard_frame.pack(fill=tk.X, pady=5, after=self.login_pass.master)
        self.login_guard.focus()

    def _login_submit_guard(self):
        code = self.login_guard.get().strip()
        if not code: return
        self.console_append("> (验证码已发送)")
        self.login_status.set("重新登录中...")
        self.login_guard_btn.configure(state=tk.DISABLED)
        pw = self.login_pass.get()
        self._start_steamcmd_with_args(["+@NoPromptForPassword", "1", "+set_steam_guard_code", code, "+login", self.login_user_val, pw])
        threading.Thread(target=self._login_wait_done, daemon=True).start()

    def _login_wait_done(self):
        try:
            self.root.after(0, lambda: self.login_status.set("正在验证..."))
            lines, matched = self.steamcmd.read_until(["Waiting for user info","Invalid Password","set_steam_guard_code","Login Failure","Login failed","Account Logon Denied"], timeout=90)
            log.info("[LOGIN] _login_wait_done: matched=%s, lines=%d", matched, len(lines))
            if matched and "Waiting" in matched: self.root.after(0, self._login_done)
            elif matched and ("set_steam_guard_code" in matched or "Account Logon Denied" in matched): self.root.after(0, lambda: self._login_reset("验证码错误，请重试"))
            elif matched and "Invalid" in matched: self.root.after(0, lambda: self._login_reset("密码错误"))
            elif matched and ("Login" in matched or "fail" in matched.lower()): self.root.after(0, lambda: self._login_reset("登录失败"))
            else: self.root.after(0, lambda: self._login_reset("超时或无响应"))
        except Exception as e:
            log.error("[LOGIN] _login_wait_done 异常: %s", e)
            self.root.after(0, lambda: self.console_append("错误: " + str(e)))

    def _login_done(self):
        self.login_guard_frame.pack_forget()
        self.console_append("=" * 40); self.console_append("✅ Steam 登录成功！"); self.console_append("=" * 40)
        self.login_form_frame.pack_forget()
        self.login_loggedin_user.configure(text=self.login_user_val)
        self.login_loggedin_frame.pack(fill=tk.X)
        self.login_status.set("")
        self.home_btns[1].configure(state=tk.NORMAL)
        self.nb.tab(self.tab_upload, state="normal")
        self.login_indicator.configure(text="● " + self.login_user_val, fg="#00cc66")
        self.ul_warn.configure(text="✅ 已登录: " + self.login_user_val, fg="#00cc66")
        self._refresh_logins()
        messagebox.showinfo("成功", "Steam 登录成功！", parent=self.root)

    def _logout(self):
        self.login_loggedin_frame.pack_forget()
        self.login_user.delete(0, tk.END)
        self.login_pass.delete(0, tk.END)
        self.login_form_frame.pack(fill=tk.X)
        self._login_reset("已退出登录")

    def _login_reset(self, msg="输入账号密码后点击登录"):
        self.login_guard_frame.pack_forget()
        self.login_status.set(msg)
        self.login_btn.configure(state=tk.NORMAL, text="开始登录")
        self.login_guard_btn.configure(state=tk.NORMAL)
        self.login_guard.delete(0, tk.END)
        self.login_user.configure(state=tk.NORMAL)
        self.login_pass.configure(state=tk.NORMAL)
        self.home_btns[1].configure(state=tk.DISABLED)
        self.nb.tab(self.tab_upload, state="disabled")
        self.login_indicator.configure(text="未登录", fg="#888")
        self.ul_warn.configure(text="⚠ 需要先登录 Steam 账号", fg="#ee4444")

    @staticmethod
    def _mask_user(u):
        if len(u) <= 6: return u[:3] + "***"
        return u[:3] + "***" + u[-3:]

    def _refresh_logins(self):
        self.login_list.delete(0, tk.END)
        for l in self.cfg.logins:
            u_enc = l.get("user", "")
            pw = l.get("pass", "")
            valid = True
            u = u_enc
            if u_enc:
                try:
                    u = _dpapi_decrypt(base64.b64decode(u_enc)).decode("utf-8")
                except:
                    valid = False
            if pw and valid:
                try:
                    _dpapi_decrypt(base64.b64decode(pw))
                except:
                    valid = False
            if not valid:
                display = (self._mask_user(u) + "  " if u else "") + "(非本机保存)"
                self.login_list.insert(tk.END, display)
            else:
                self.login_list.insert(tk.END, u)

    def _login_from_saved(self, e=None):
        sel = self.login_list.curselection()
        if not sel: return
        ls = self.cfg.logins
        if sel[0] < len(ls):
            entry = ls[sel[0]]
            pw = entry.get("pass", "")
            if pw:
                try:
                    _dpapi_decrypt(base64.b64decode(pw))
                except:
                    messagebox.showwarning("无法登录", "非本机保存，请重新登录", parent=self.root)
                    return
            u = decrypt_pass(entry.get("user", ""))
            self.login_user.delete(0, tk.END); self.login_user.insert(0, u)
            self.login_pass.delete(0, tk.END)
            pw_dec = decrypt_pass(pw)
            if pw_dec: self.login_pass.insert(0, pw_dec)
            self.login_user.configure(state=tk.DISABLED)
            self.login_pass.configure(state=tk.DISABLED)
            self._login_start()

    def _save_login(self):
        u = self.login_user.get().strip(); p = self.login_pass.get()
        if u: self.cfg.add_login(u, p); self._refresh_logins(); messagebox.showinfo("成功", "已保存")

    def _delete_login(self):
        sel = self.login_list.curselection()
        if not sel: return
        self.cfg.del_login(sel[0]); self._refresh_logins()

    def _clear_creds(self):
        def run():
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            path = SteamCMD.find_path()
            if not path: return
            subprocess.run([path, "+clear_credentials", "+quit"],
                capture_output=True, timeout=30, startupinfo=si,
                creationflags=subprocess.CREATE_NO_WINDOW, cwd=os.path.dirname(os.path.abspath(path)))
            self.root.after(0, lambda: messagebox.showinfo("成功", "缓存已清除"))
        threading.Thread(target=run, daemon=True).start()

    # ── 设置 ──────────────────────────────
    def _build_settings(self):
        f = self.tab_settings
        af = ttk.LabelFrame(f, text="常用 App 列表管理"); af.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        cols = ("id","name","show","note")
        self.app_tree = ttk.Treeview(af, columns=cols, show="headings", height=8)
        self.app_tree.heading("id", text="App ID"); self.app_tree.heading("name", text="名称"); self.app_tree.heading("show", text="显示"); self.app_tree.heading("note", text="备注")
        self.app_tree.column("id", width=80); self.app_tree.column("name", width=160); self.app_tree.column("show", width=50); self.app_tree.column("note", width=150)
        self.app_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        sc = ttk.Scrollbar(af, orient="vertical", command=self.app_tree.yview)
        self.app_tree.configure(yscrollcommand=sc.set); sc.pack(side=tk.RIGHT, fill=tk.Y)
        bf2 = ttk.Frame(af); bf2.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(bf2, text="➕ 添加", command=self._add_app).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf2, text="🗑 删除", command=self._remove_app).pack(side=tk.LEFT, padx=2)
        self.app_tree.bind("<ButtonRelease-1>", self._on_app_click)
        self.app_tree.bind("<Double-Button-1>", self._edit_app)
        self._refresh_apps()

        uf = ttk.LabelFrame(f, text="更新"); uf.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(uf, text=f"当前版本: {__version__}", font=("微软雅黑",9), fg="#888").pack(pady=(5,0))
        ttk.Button(uf, text="检查更新", command=self._check_update).pack(pady=5)

    def _refresh_apps(self):
        log.debug("[REFRESH] 开始刷新 app 列表")
        for i in self.app_tree.get_children(): self.app_tree.delete(i)
        apps = self.cfg.apps
        log.debug("[REFRESH] apps=%s", apps)
        for a in apps:
            self.app_tree.insert("", tk.END, values=(a["id"], a["name"], "✓" if a.get("show",True) else "✗", a.get("note", "")))
        log.debug("[REFRESH] 完成")

    def _add_app(self):
        d = tk.Toplevel(self.root); d.title("添加")
        self._center(d, 350, 250)
        d.transient(self.root); d.grab_set()
        tk.Label(d, text="添加 App", font=("微软雅黑",11)).pack(pady=10)
        f1 = ttk.Frame(d); f1.pack(pady=5)
        tk.Label(f1, text="App ID:").pack(side=tk.LEFT)
        eid = ttk.Entry(f1, width=15); eid.pack(side=tk.LEFT, padx=5)
        f2 = ttk.Frame(d); f2.pack(pady=5)
        tk.Label(f2, text="名称:").pack(side=tk.LEFT)
        ename = ttk.Entry(f2, width=25); ename.pack(side=tk.LEFT, padx=5)
        f3 = ttk.Frame(d); f3.pack(pady=5)
        tk.Label(f3, text="备注:").pack(side=tk.LEFT)
        enote = ttk.Entry(f3, width=25); enote.pack(side=tk.LEFT, padx=5)
        def do():
            aid = eid.get().strip()
            if aid:
                apps = self.cfg.apps
                if any(str(a["id"])==aid for a in apps): messagebox.showinfo("提示", "已存在"); d.destroy(); return
                apps.append({"id":aid,"name":ename.get().strip() or "App "+aid,"show":True,"note":enote.get().strip()})
                self.cfg.apps = apps; self._refresh_apps(); d.destroy()
        ttk.Button(d, text="添加", command=do).pack(pady=10)

    def _on_app_click(self, event):
        col = self.app_tree.identify_column(event.x)
        if col != "#3": return
        sel = self.app_tree.selection()
        if not sel: return
        item = self.app_tree.item(sel[0]); aid = str(item["values"][0])
        apps = self.cfg.apps
        for a in apps:
            if str(a["id"]) == aid: a["show"] = not a.get("show", True); break
        self.cfg.apps = apps; self._refresh_apps()

    def _edit_app(self, event):
        sel = self.app_tree.selection()
        if not sel: return
        item = self.app_tree.item(sel[0])
        vals = item["values"]
        if not vals: return
        aid, name, note = vals[0], vals[1], vals[3] if len(vals) > 3 else ""
        iid = sel[0]
        d = tk.Toplevel(self.root); d.title("编辑 App")
        self._center(d, 350, 250)
        d.transient(self.root); d.grab_set()
        tk.Label(d, text="编辑 App", font=("微软雅黑",11)).pack(pady=10)
        f1 = ttk.Frame(d); f1.pack(pady=5)
        tk.Label(f1, text="App ID:").pack(side=tk.LEFT)
        eid = ttk.Entry(f1, width=15); eid.insert(0, aid); eid.pack(side=tk.LEFT, padx=5)
        f2 = ttk.Frame(d); f2.pack(pady=5)
        tk.Label(f2, text="名称:").pack(side=tk.LEFT)
        ename = ttk.Entry(f2, width=25); ename.insert(0, name); ename.pack(side=tk.LEFT, padx=5)
        f3 = ttk.Frame(d); f3.pack(pady=5)
        tk.Label(f3, text="备注:").pack(side=tk.LEFT)
        enote = ttk.Entry(f3, width=25); enote.insert(0, note); enote.pack(side=tk.LEFT, padx=5)
        def do():
            new_id = eid.get().strip(); new_name = ename.get().strip()
            if not new_id: return
            apps = self.cfg.apps
            new_apps = []
            for a in apps:
                if str(a["id"]) == str(aid):
                    new_apps.append({"id": new_id, "name": new_name or "App "+new_id, "show": a.get("show", True), "note": enote.get().strip()})
                else:
                    new_apps.append(a)
            self.cfg.apps = new_apps
            self._refresh_apps()
            d.destroy()
        btn = ttk.Button(d, text="保存", command=do)
        btn.pack(pady=10)
        btn.focus()
        d.bind("<Return>", lambda e: do())
        eid.bind("<Return>", lambda e: ename.focus())
        ename.bind("<Return>", lambda e: enote.focus())
        enote.bind("<Return>", lambda e: do())

    def _remove_app(self):
        sel = self.app_tree.selection()
        if not sel: return
        item = self.app_tree.item(sel[0]); aid = item["values"][0]
        if messagebox.askyesno("确认", "删除 [{}]?".format(aid)):
            self.cfg.apps = [a for a in self.cfg.apps if str(a["id"]) != str(aid)]; self._refresh_apps()

    def _check_update_startup(self):
        threading.Thread(target=self._do_check_update, args=(True,), daemon=True).start()

    def _check_update(self):
        self.status.configure(text="正在检查更新...")
        threading.Thread(target=self._do_check_update, args=(False,), daemon=True).start()

    def _do_check_update(self, silent=False):
        try:
            import urllib.request
            resp = urllib.request.urlopen(VERSION_URL, timeout=15)
            content = resp.read().decode("utf-8")
            m = re.search(r'__version__\s*=\s*"([^"]+)"', content)
            if not m:
                if not silent:
                    self.root.after(0, lambda: messagebox.showinfo("检查更新", "无法解析远程版本号", parent=self.root))
                self.root.after(0, lambda: self.status.configure(text="就绪"))
                return
            remote_ver = m.group(1)
            if remote_ver == __version__:
                if not silent:
                    self.root.after(0, lambda: messagebox.showinfo("检查更新", f"已是最新版本 ({__version__})", parent=self.root))
                self.root.after(0, lambda: self.status.configure(text="就绪"))
                return
            self._update_available = (remote_ver, content)
            self.root.after(0, self._show_update_bar)
            if not silent:
                self.root.after(0, self._show_update_dialog)
        except Exception as e:
            if not silent:
                self.root.after(0, lambda: messagebox.showerror("检查更新", f"更新检查失败:\n{e}", parent=self.root))
            self.root.after(0, lambda: self.status.configure(text="就绪"))

    def _show_update_bar(self):
        self._update_bar.pack(fill=tk.X, before=self._console_frame)

    def _show_update_dialog(self):
        if not self._update_available:
            return
        remote_ver, content = self._update_available
        if not messagebox.askyesno("发现新版本",
                f"当前版本: {__version__}\n最新版本: {remote_ver}\n\n是否更新？",
                parent=self.root):
            return
        self._do_update(content)
    def _do_update(self, content):
        try:
            self_path = Path(__file__).resolve()
            new_path = self_path.with_suffix(".py.new")
            new_path.write_text(content, encoding="utf-8")
            bat_path = self_path.with_name("_updater.bat")
            bat_content = f"""@echo off
timeout /t 1 /nobreak >nul
copy /y "{new_path}" "{self_path}" >nul
del "{new_path}"
start "" pythonw "{self_path}"
del "%~f0"
"""
            bat_path.write_text(bat_content, encoding="utf-8")
            subprocess.Popen(["start", "", str(bat_path)], shell=True)
            self.root.quit()
        except Exception as e:
            messagebox.showerror("更新失败", str(e), parent=self.root)

    def run(self):
        self.root.mainloop()

# ── 入口 ──────────────────────────────────
if __name__ == "__main__":
    App().run()
