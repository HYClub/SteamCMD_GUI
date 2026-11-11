#!/usr/bin/env python3
"""SteamCMD GUI — 单文件版"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont
import subprocess, threading, queue, os, logging, time, traceback, re, json, base64
import ctypes, ctypes.wintypes
import sys, shutil, zipfile, urllib.request, urllib.error
from pathlib import Path

__version__ = "20261111"
VERSION_URL = "https://raw.githubusercontent.com/HYClub/SteamCMD_GUI/master/steamcmd_gui.py"

# ── 路径基准 ──────────────────────────────
# 打成 exe（PyInstaller onefile）后 __file__ 指向 C:\Users\...\Temp\_MEIxxxx，每次运行都换
# 一个目录。配置、日志、output、scripts 要是还按 __file__ 算，就等于写进垃圾桶：关掉程序
# 全没，下次启动又是全新的。所以凡是「本程序旁边」的路径，一律走 APP_DIR；
# 要复制/替换「本程序自己」时走 SELF_PATH（exe 模式下是那个 exe，不是临时解压出来的 py）。
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SELF_PATH = Path(sys.executable).resolve() if FROZEN else Path(__file__).resolve()

# ── 多语言 ────────────────────────────────
# 中文原文直接当 key，查不到就原样返回 —— 漏翻最多露出中文，不会崩。
# 英文词表放在文件末尾（_EN.update），避免把三千行程序的开头塞满翻译。
LANG = "zh"
LANGS = (("zh", "简体中文"), ("en", "English"))
_EN = {}
_EN_REV = {}      # 英文 -> 中文，切回中文时把运行时已经是英文的状态文字翻回去

def tr(s):
    """取当前语言的文案。

    中文原文当 key：英文模式查不到就原样返回。反查表让「已经翻成英文的运行时文字」
    在切回中文时也能变回来，否则状态栏会留一句英文。

    中文模式先判断「它本来就是中文 key」：有几组不同中文对应同一个英文（登录/去登录/开始登录
    都是 Sign in），反查表只能留下其中一个，直接拿反查表会把原话改掉。是中文就原样用。
    """
    if LANG == "en":
        return _EN.get(s, s)
    if s in _EN:
        return s
    return _EN_REV.get(s, s)

def set_lang(code):
    global LANG
    if code in ("zh", "en"):
        LANG = code

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
        raise RuntimeError(tr("DPAPI 加密失败"))
    data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return data

def _dpapi_decrypt(encrypted: bytes) -> bytes:
    blob_in = _DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
    blob_out = _DATA_BLOB()
    if not _P_CRYPTUNPROTECT(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError(tr("DPAPI 解密失败"))
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
LOG_DIR = APP_DIR
LOG_FILE = LOG_DIR / "steamcmd_gui.log"
if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1024 * 1024:
    LOG_FILE.write_text("")

# 日志只记「做了什么」：启动、发了哪条命令、结果如何。终端回显的内容一律不落盘——
# 密码提示、验证码输入都在终端里，写进去就等于存档。另外再加一道兜底：
# 运行期把真正用过的密码 / 验证码登记进来，万一哪天有人手滑把它塞进日志，落盘前也会被抹掉。
_REDACT_VALUES = set()

def remember_secret(v):
    """登记敏感值（密码 / 验证码），日志与控制台输出都会把它抹成 ***。"""
    if v and len(v) >= 3:
        _REDACT_VALUES.add(v)

def redact(text):
    """按值抹掉已知敏感内容。

    不猜格式、不看上下文：只要这个字符串是本次运行真正用过的密码 / 验证码，
    出现在哪儿都会被替换。这样即使哪天 SteamCMD 把输入回显出来，控制台也看不到。
    """
    if not text or not _REDACT_VALUES:
        return text
    for v in _REDACT_VALUES:
        if v in text:
            text = text.replace(v, "***")
    return text

def scrub_log(text):
    return redact(mask_secrets(text or ""))

class _RedactHandler(logging.FileHandler):
    """落盘前再抹一遍：任何已登记过的敏感值都不会出现在日志里。"""

    def emit(self, record):
        try:
            msg = scrub_log(self.format(record))
            self.stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

try:
    _log_handler = _RedactHandler(LOG_FILE, encoding="utf-8")
except Exception:
    _log_handler = logging.NullHandler()      # 目录只读也不该拖垮启动

logging.basicConfig(
    # 默认 INFO：日志只记「做了什么」。排查问题再设 STEAMCMD_GUI_DEBUG=1 开 DEBUG。
    level=logging.DEBUG if os.environ.get("STEAMCMD_GUI_DEBUG") else logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    handlers=[_log_handler],
)
log = logging.getLogger("steamcmd_gui")

# ── 默认 App 列表 ─────────────────────────
DEFAULT_APPS = []

# ── 工具函数 ──────────────────────────────
def strip_ansi(text):
    return ANSI_RE.sub('', text)

def _ver_tuple(v):
    """把版本号拆成可比较的元组：数字段和字母段分开打包。

    每段统一成 (类型, 值)：数字 -> (1, 整数)，字母 -> (0, 小写串)。
    这样元组比较时先比类型标记，永远不会出现 int 和 str 直接比大小而炸掉。
    """
    parts = re.findall(r"\d+|[A-Za-z]+", str(v or ""))
    return tuple((1, int(p)) if p.isdigit() else (0, p.lower()) for p in parts)

def ver_is_newer(remote, local):
    """远端版本是否比本地新。只比大小，不猜「不相等就是有更新」。"""
    return _ver_tuple(remote) > _ver_tuple(local)

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
        # 配置放在「本程序旁边」，文件名固定：py 版和 exe 版读同一份，换着跑不会各存一套
        self.path = APP_DIR / "steamcmd_gui.cfg"
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
    @property
    def lang(self): return self.data.get("lang","zh")
    @lang.setter
    def lang(self,v):
        self.data["lang"]=v; self.save()
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

# ── 敏感信息脱敏 ────────────────────────
# 只认「命令行形式」的写法（带 + 前缀）。交互式敲的 `login 用户名` 后面跟的不是密码，
# 而 SteamCMD 自己的提示 "Proceeding with login using username/password." 以前会被这条
# 正则误伤成 "login using ***"。真正的密码靠 remember_secret() 按值抹，比猜格式靠谱。
_SECRET_PATTERNS = [
    # +login 用户名 密码
    (re.compile(r"(\+login\s+\S+)(\s+\S+)", re.IGNORECASE), r"\1 ***"),
    # +set_steam_guard_code 12345
    (re.compile(r"(\+set_steam_guard_code)(\s+\S+)", re.IGNORECASE), r"\1 ***"),
]


def mask_secrets(text):
    """把命令行里的密码 / 验证码替换成 ***，用于日志与界面回显。"""
    if not text:
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ── ConPTY 封装 ──────────────────────────
for _ in range(2):
    try:
        import winpty
        break
    except ImportError:
        import subprocess, sys, os
        try:
            # 打包成 exe 后没有 pip 可用，而且 winpty 是随包打进去的，走到这里说明打包本身就漏了
            if not FROZEN:
                si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; si.wShowWindow = subprocess.SW_HIDE
                subprocess.run([sys.executable, "-m", "pip", "install", "pywinpty", "-q"],
                    timeout=60, startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW,
                    capture_output=True)
        except Exception:
            pass
else:
    import tkinter.messagebox as mb
    mb.showerror(tr("错误"), tr("缺少 pywinpty，自动安装失败，请手动运行:\n\npip install pywinpty"))
    raise SystemExit(1)

class ConPTY:
    def __init__(self, cols=120, rows=30):
        self.cols = cols; self.rows = rows; self._pty = None

    def start(self, exe_path, cwd=None, extra_args=None):
        if extra_args:
            cmdline = exe_path + " " + " ".join(extra_args)
        else:
            cmdline = None
        log.info("[ConPTY] 启动 cmdline=%s cwd=%s", mask_secrets(cmdline) or exe_path, cwd)
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
            if self.on_output: self.on_output(tr("ConPTY 启动失败"))
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

    def _mask(self, text):
        """控制台回显脱敏（统一走模块级 mask_secrets）。"""
        return mask_secrets(text)

    def _push(self, data):
        if not data: return
        text = strip_ansi(data.decode("utf-8", errors="replace")).replace("\r", "")
        if not text.strip(): return
        masked = redact(self._mask(text))          # 控制台也不许出现密码 / 验证码
        log.debug("[push] %d bytes", len(text))    # 只记长度，终端内容不进日志
        self._buf.put(masked); self._first_push.set()
        if self.on_output:
            try: self.on_output(masked)
            except Exception as e: log.error("[push] on_output 回调异常: %s", e)

    def send(self, cmd):
        if not self._conpty or not self._conpty.is_alive(): return False
        try:
            raw = (cmd + "\n").encode("utf-8")
            log.info("[send] >>> %s (raw=%d bytes)", mask_secrets(cmd), len(raw))
            return self._conpty.write(raw)
        except Exception as e: log.error("[send] 失败: %s", e); return False

    def send_secret(self, text, terminator="\r"):
        """发送敏感内容（密码 / 验证码）：原文不进日志、不做字符串拼接回显。

        密码输入提示（`password:`）用 \\r 提交才会被接受，命令行走 \\n。
        """
        if not self._conpty or not self._conpty.is_alive(): return False
        try:
            raw = (text + terminator).encode("utf-8")
            remember_secret(text)          # 登记：之后任何日志里出现它都会被抹成 ***
            log.info("[send] >>> *** (secret, %d bytes)", len(raw))
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
    def find_path(extra=None):
        """按优先级查找 steamcmd.exe：显式路径 → 本程序目录 → 上级目录 → 当前工作目录。"""
        here = APP_DIR
        cands = []
        if extra:
            cands.append(Path(extra))
        cands += [here / "steamcmd.exe", here.parent / "steamcmd.exe",
                  Path.cwd() / "steamcmd.exe"]
        for p in cands:
            try:
                if p.is_file():
                    return str(p)
            except OSError:
                continue
        return None

# ── UI 常量（与 Steam 主题保持一致）──────
UI_BG_DARK   = "#171a21"
UI_BG_MID    = "#1b2838"
UI_BG_LIGHT  = "#2a475e"
UI_BORDER    = "#2a3f5a"
UI_ACCENT    = "#66c0f4"
UI_ACCENT_BG = "#417a9b"
UI_ACCENT_HV = "#4ba8d6"
UI_WARN      = "#e8a735"
UI_GREEN     = "#5ba32b"
UI_TEXT      = "#c7d5e0"
UI_TEXT_DIM  = "#8f98a0"
UI_ENTRY     = "#11151c"
UI_LOG_BG    = "#0d1117"
UI_WARN_BG   = "#221f14"
UI_GREEN_BG  = "#16210f"

F_TXT   = ("Microsoft YaHei", 9)
F_TXT_S = ("Microsoft YaHei", 8)
F_BOLD  = ("Microsoft YaHei", 10, "bold")
F_MONO  = ("Consolas", 9)

STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAMCMD_DOC_URL = "https://developer.valvesoftware.com/wiki/SteamCMD"


# ── SteamCMD 安装器（下载 / 解压 / 初始化 / 复制自身）──
class SteamCmdInstaller:
    """下载解压 steamcmd.zip，并支持把本程序复制到目标目录后重启。"""

    @staticmethod
    def download(url, dest, on_progress=None, on_log=None, cancel=None):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "SteamCMD-GUI"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError(tr("已取消下载"))
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        return done

    @staticmethod
    def extract(zip_path, dest):
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            z.extractall(dest)
        # 定位 steamcmd.exe（可能在子目录里）
        found = None
        for root, _dirs, files in os.walk(dest):
            for fn in files:
                if fn.lower() == "steamcmd.exe":
                    found = os.path.join(root, fn)
                    break
            if found:
                break
        if not found:
            return None
        sub = os.path.dirname(os.path.abspath(found))
        if sub != os.path.abspath(dest):
            for name in os.listdir(sub):
                shutil.move(os.path.join(sub, name), os.path.join(dest, name))
            found = os.path.join(dest, "steamcmd.exe")
        return found if os.path.isfile(found) else None

    @staticmethod
    def initialize(exe, on_log=None, cancel=None, timeout=900):
        """运行 steamcmd +quit 完成首次自更新（拉取运行环境）。"""
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(
            [exe, "+quit"], cwd=os.path.dirname(exe),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            startupinfo=si, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        start = time.time()
        try:
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    raise InterruptedError(tr("已取消初始化"))
                if time.time() - start > timeout:
                    proc.kill()
                    raise TimeoutError(tr("初始化超时"))
                line = strip_ansi(line.rstrip("\r\n"))
                if line.strip() and on_log:
                    on_log(line)
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
        return proc.returncode

    @staticmethod
    def self_path():
        # exe 模式下 __file__ 是临时解压目录里的 py，复制它等于复制空气 —— 要复制的是 exe 自己
        return str(SELF_PATH)

    @classmethod
    def copy_self(cls, target_dir, with_cfg=True):
        """把本程序（及可选配置）复制到目标目录，返回实际写入的文件列表。"""
        written = []
        src = cls.self_path()
        dst = os.path.join(target_dir, os.path.basename(src))
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(src):
            return written
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(dst):
            try:
                os.replace(dst, dst + ".bak")
            except OSError:
                pass
        shutil.copy2(src, dst)
        written.append(dst)

        if with_cfg:
            cfg = str(APP_DIR / "steamcmd_gui.cfg")
            dcfg = os.path.join(target_dir, os.path.basename(cfg))
            if os.path.isfile(cfg) and os.path.normcase(os.path.abspath(dcfg)) != os.path.normcase(cfg):
                if os.path.exists(dcfg):
                    try:
                        os.replace(dcfg, dcfg + ".bak")
                    except OSError:
                        pass
                shutil.copy2(cfg, dcfg)
                written.append(dcfg)
        return written

    @staticmethod
    def relaunch(target_dir):
        """在新目录重新启动本程序。"""
        target = os.path.join(target_dir, os.path.basename(SteamCmdInstaller.self_path()))
        if FROZEN:
            # exe 自己就是可执行文件，直接拉起复制过去的那一份，不要再套一层解释器
            cmd = [target]
        else:
            exe = sys.executable
            if exe.lower().endswith("python.exe"):
                cand = exe[: -len("python.exe")] + "pythonw.exe"
                if os.path.isfile(cand):
                    exe = cand
            cmd = [exe, target]
        subprocess.Popen(
            cmd, cwd=target_dir,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


# ── 通用 UI 组件 ─────────────────────────
def _mix_hex(a, b, t):
    """两色线性混合，t=0 取 a，t=1 取 b。"""
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


# Pillow 可选：有就用超采样渲染出带抗锯齿的圆角，没有就退回 Canvas 直绘
try:
    from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageTk as _PILImageTk
    _HAS_PIL = True
except Exception:                                   # pragma: no cover
    _HAS_PIL = False

_AA_CACHE = {}


def _pil_btn_image(w, h, r, ctop, cbot, border):
    """超采样渲染一个抗锯齿圆角按钮底图，结果按参数缓存。"""
    key = (w, h, r, ctop, cbot, border)
    img = _AA_CACHE.get(key)
    if img is not None:
        return img

    s = 4                                           # 4x 超采样
    W, H = max(1, w * s), max(1, h * s)
    rad = max(1, int(r * s))

    top = tuple(int(ctop[i:i + 2], 16) for i in (1, 3, 5))
    bot = tuple(int(cbot[i:i + 2], 16) for i in (1, 3, 5))

    grad = _PILImage.new("RGB", (W, H))
    gd = _PILDraw.Draw(grad)
    for y in range(H):
        t = y / max(1, H - 1)
        gd.line([(0, y), (W, y)],
                fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

    mask = _PILImage.new("L", (W, H), 0)
    _PILDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=rad, fill=255)

    im = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    im.paste(grad, (0, 0), mask)
    if border:
        bcol = tuple(int(border[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        h2 = s // 2                      # 描边居中压在轮廓线上，降采样后正好 1px
        _PILDraw.Draw(im).rounded_rectangle(
            [h2, h2, W - 1 - h2, H - 1 - h2], radius=rad,
            outline=bcol, width=s)

    im = im.resize((w, h), _PILImage.LANCZOS)
    photo = _PILImageTk.PhotoImage(im)
    _AA_CACHE[key] = photo
    return photo


class RoundButton(tk.Canvas):
    """Canvas 自绘圆角按钮 —— tk/ttk 原生按钮不支持圆角。

    配色/圆角对齐 HTML 原型：.btn 为 #67c1f5→#417a9b 竖向渐变、圆角 3px；
    .btn-secondary 为半透明蓝底 + 1px 描边；.btn-green 为绿色渐变。
    """

    # kind: (顶部色, 底部色, hover顶, hover底, 文字色, 描边色, 填充方式)
    _PALETTE = {
        "primary":   ("#67c1f5", "#417a9b", "#8ed4ff", "#4ba8d6", "#ffffff", "", "grad"),
        "secondary": ("#2a475e", "#2a475e", "#396584", "#396584", UI_ACCENT, UI_ACCENT, "flat"),
        "ghost":     (UI_ENTRY, UI_ENTRY, UI_BG_LIGHT, UI_BG_LIGHT, UI_TEXT_DIM, UI_BORDER, "flat"),
        "normal":    (UI_BG_LIGHT, UI_BG_LIGHT, "#33556f", "#33556f", UI_TEXT, UI_BORDER, "flat"),
        "green":     ("#75b022", "#4a7513", "#8dcc2a", "#5c9318", "#ffffff", "", "grad"),
    }

    def __init__(self, parent, text="", command=None, kind="primary",
                 minw=0, height=30, radius=4, font=None, bg=None):
        self._text = text
        self._cmd = command
        self._kind = kind if kind in self._PALETTE else "primary"
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._radius = radius
        self._height = height
        self._font = font or F_TXT
        try:
            pbg = bg or parent.cget("bg")
        except tk.TclError:
            pbg = UI_BG_MID
        self._parent_bg = pbg
        w = max(minw, tkfont.Font(font=self._font).measure(text) + 34)
        super().__init__(parent, width=w, height=height, bg=pbg,
                         highlightthickness=0, bd=0, takefocus=0)
        self._btn_w = w
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        if self._enabled:
            self.configure(cursor="hand2")

    # ── 绘制 ──
    def _rr(self, x1, y1, x2, y2, r, fill):
        """填充圆角矩形（矩形主体 + 四段 90° 扇形补角）。

        d 取 2r-1：tk 的坐标是「包含端点」的，用 2r 会在角上多出 1px，
        视觉上就是用户截图里那种方形小毛刺。
        """
        r = max(1, int(r))
        self.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
        self.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")
        d = r * 2 - 1
        for ax, ay, st in ((x1, y1, 90), (x2 - d, y1, 0),
                           (x1, y2 - d, 180), (x2 - d, y2 - d, 270)):
            self.create_arc(ax, ay, ax + d, ay + d, start=st, extent=90,
                            fill=fill, outline="", style=tk.PIESLICE)

    def _stroke_rr(self, x1, y1, x2, y2, r, color):
        """1px 圆角描边：4 段直线 + 4 段 ARC 曲线。

        不再用「外圈填描边色 + 内圈填底色」那套——半径 3 时内外圆弧半径差
        会在角上留下断口和台阶，放大看就是缺角。
        """
        r = max(1, int(r))
        d = r * 2 - 1
        self.create_line(x1 + r, y1, x2 - r, y1, fill=color, width=1)
        self.create_line(x1 + r, y2, x2 - r, y2, fill=color, width=1)
        self.create_line(x1, y1 + r, x1, y2 - r, fill=color, width=1)
        self.create_line(x2, y1 + r, x2, y2 - r, fill=color, width=1)
        for ax, ay, st in ((x1, y1, 90), (x2 - d, y1, 0),
                           (x1, y2 - d, 180), (x2 - d, y2 - d, 270)):
            self.create_arc(ax, ay, ax + d, ay + d, start=st, extent=90,
                            style=tk.ARC, outline=color, width=1)

    def _grad_rr(self, x1, y1, x2, y2, r, ctop, cbot):
        """竖向渐变 + 圆角：逐行画矩形，行左右内缩量按圆角圆弧计算。"""
        span = max(1.0, float(y2 - y1))
        y = y1
        while y < y2:
            t = (y - y1) / span
            col = _mix_hex(ctop, cbot, t)
            inset = 0.0
            if y < y1 + r:
                dy = (y1 + r) - (y + 0.5)
                if dy > 0:
                    inset = r - max(0.0, r * r - dy * dy) ** 0.5
            elif y > y2 - r:
                dy = (y + 0.5) - (y2 - r)
                if dy > 0:
                    inset = r - max(0.0, r * r - dy * dy) ** 0.5
            self.create_rectangle(x1 + inset, y, x2 - inset, y + 1, fill=col, outline="")
            y += 1

    def _draw(self):
        self.delete("all")
        w, h, r = self._btn_w, self._height, self._radius
        if not self._enabled:
            top = bot = UI_ENTRY
            txt, border, style = "#5a6b7a", UI_BORDER, "flat"
        else:
            top, bot, htop, hbot, txt, border, style = self._PALETTE[self._kind]
            if self._hover:
                top, bot = htop, hbot
            if self._pressed:
                top = bot = _mix_hex(bot, "#000000", 0.15)
        off = 1 if self._pressed else 0
        x1, y1, x2, y2 = 1, 1 + off, w - 2, h - 2 + off
        if _HAS_PIL:
            # 抗锯齿路径：整块底图（渐变 + 圆角 + 描边）一次画好
            img = _pil_btn_image(x2 - x1 + 1, y2 - y1 + 1, r, top, bot,
                                 border or None)
            self.create_image(x1, y1, image=img, anchor="nw")
        elif style == "grad":
            self._grad_rr(x1, y1, x2, y2, r, top, bot)
        else:
            self._rr(x1, y1, x2, y2, r, top)
        if border and not _HAS_PIL:
            self._stroke_rr(x1, y1, x2, y2, r, border)
        self.create_text(w // 2, h // 2 + off, text=self._text, fill=txt, font=self._font)

    # ── 交互 ──
    def _on_enter(self, _e):
        if not self._enabled:
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, e):
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and 0 <= e.x <= self._btn_w and 0 <= e.y <= self._height and self._cmd:
            self._cmd()

    # ── 兼容 Button 接口 ──
    def set_kind(self, kind):
        """切换视觉类型（primary / secondary / ghost / normal / green）。"""
        if kind in self._PALETTE and kind != self._kind:
            self._kind = kind
            self._draw()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        changed = False
        if "text" in kw:
            self._text = kw.pop("text")
            changed = True
        if "state" in kw:
            st = kw.pop("state")
            self._enabled = st not in (tk.DISABLED, "disabled")
            self.configure(cursor="hand2" if self._enabled else "")
            changed = True
        if kw:
            super().configure(**kw)
        if changed:
            self._draw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)


def ui_btn(parent, text, kind="primary", cmd=None, width=None):
    k = kind if kind in RoundButton._PALETTE else "primary"
    return RoundButton(parent, text=text, command=cmd, kind=k,
                       minw=(width * 8 if width else 0))


def rb(parent, text, command=None, kind="normal", minw=0):
    """ttk.Button 的圆角替代品。"""
    return RoundButton(parent, text=text, command=command, kind=kind, minw=minw)


def ui_btn_enable(b, on):
    """切换按钮启用态。"""
    b.configure(state=tk.NORMAL if on else tk.DISABLED)


def ui_link(parent, text, url):
    lb = tk.Label(parent, text=text, bg=UI_BG_MID, fg=UI_ACCENT, font=F_TXT_S, cursor="hand2")
    lb.bind("<Button-1>", lambda e: __import__("webbrowser").open(url))
    lb.bind("<Enter>", lambda e: lb.configure(fg="#ffffff"))
    lb.bind("<Leave>", lambda e: lb.configure(fg=UI_ACCENT))
    return lb


def _fill_round(cv, x1, y1, x2, y2, r, fill):
    """在 Canvas 上填充圆角矩形（矩形主体 + 四段 90° 扇形补角）。"""
    r = max(1, int(r))
    d = r * 2 - 1
    cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
    cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")
    for ax, ay, st in ((x1, y1, 90), (x2 - d, y1, 0),
                       (x1, y2 - d, 180), (x2 - d, y2 - d, 270)):
        cv.create_arc(ax, ay, ax + d, ay + d, start=st, extent=90,
                      fill=fill, outline="", style=tk.PIESLICE)


class SteamScrollbar(tk.Canvas):
    """细身暗色滚动条。

    clam 主题的滚动条是「贴图元素」，`configure('TScrollbar', background=...)`
    根本不生效（滑块永远是一块浅灰），所以这里自绘。
    接口与 ttk.Scrollbar 兼容：把 `.set` 交给 yscrollcommand 即可。
    """

    def __init__(self, parent, command=None, width=10, bg=UI_BG_MID,
                 thumb="#3d5878", thumb_hover=UI_ACCENT):
        super().__init__(parent, width=width, height=1, bg=bg, highlightthickness=0,
                         bd=0, takefocus=0)
        self._cmd = command
        self._bar_w = width
        self._thumb = thumb
        self._thumb_hv = thumb_hover
        self._bg = bg
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._drag_off = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    # ── 与 ttk.Scrollbar 兼容 ──
    def set(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            return
        self._draw()

    def get(self):
        return self._first, self._last

    # ── 几何 ──
    def _track(self):
        return 2, max(2, self.winfo_height() - 2)

    def _thumb_box(self):
        top, bottom = self._track()
        span = bottom - top
        y1 = top + self._first * span
        y2 = top + self._last * span
        minh = 28
        if y2 - y1 < minh:                      # 内容越长滑块越短，但不小于 28px
            y2 = min(bottom, y1 + minh)
            y1 = max(top, y2 - minh)
        return y1, y2

    def _draw(self):
        self.delete("all")
        w = self._bar_w
        if self._last - self._first >= 0.999:   # 不需要滚动
            return
        y1, y2 = self._thumb_box()
        col = self._thumb_hv if (self._hover or self._drag_off is not None) else self._thumb
        _fill_round(self, 2, y1, w - 2, y2, max(2, (w - 4) // 2), col)

    # ── 交互 ──
    def _on_press(self, e):
        y1, y2 = self._thumb_box()
        if y1 <= e.y <= y2:
            self._drag_off = e.y - y1
        else:                                   # 点空白 = 翻页
            self._drag_off = None
            if self._cmd:
                self._cmd("scroll", 1 if e.y > y2 else -1, "pages")
        self._draw()

    def _on_drag(self, e):
        if self._drag_off is None:
            return
        top, bottom = self._track()
        span = float(bottom - top)
        frac = self._last - self._first
        if span <= 0 or frac >= 1:
            return
        pos = (e.y - self._drag_off - top) / span
        pos = max(0.0, min(1.0 - frac, pos))
        if self._cmd:
            self._cmd("moveto", pos)

    def _on_release(self, _e):
        self._drag_off = None
        self._draw()

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()


class ScrollFrame(tk.Frame):
    """竖向滚动容器：内容超出可视区时自动显示滚动条。

    用法：sf = ScrollFrame(page); sf.pack(fill=BOTH, expand=True);
         之后把控件放进 sf.body 即可。
    """

    def __init__(self, parent, bg=UI_BG_MID):
        super().__init__(parent, bg=bg)
        self._cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=0)
        self._sb = SteamScrollbar(self, command=self._cv.yview, bg=bg)
        self._cv.configure(yscrollcommand=self._sb.set)
        self._cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._sb.pack_forget()

        self.body = tk.Frame(self._cv, bg=bg)
        self._win = self._cv.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._sync_region)
        self._cv.bind("<Configure>", self._sync_width)
        self._top = self.winfo_toplevel()
        self._top.bind("<MouseWheel>", self._on_wheel, add="+")

    # ── 尺寸同步 ──
    def _sync_width(self, e):
        self._cv.itemconfigure(self._win, width=e.width)
        self._sync_region()

    def _sync_region(self, _e=None):
        box = self._cv.bbox("all")
        if not box:
            return
        self._cv.configure(scrollregion=box)
        need = box[3] > self._cv.winfo_height() + 1
        if need and not self._sb.winfo_ismapped():
            self._sb.pack(side=tk.RIGHT, fill=tk.Y)
        elif not need and self._sb.winfo_ismapped():
            self._sb.pack_forget()
            self._cv.yview_moveto(0)

    # ── 滚轮 ──
    def _on_wheel(self, e):
        if not self.winfo_ismapped():
            return
        x, y = self.winfo_rootx(), self.winfo_rooty()
        if not (x <= e.x_root <= x + self.winfo_width()
                and y <= e.y_root <= y + self.winfo_height()):
            return
        w = self.winfo_containing(e.x_root, e.y_root)
        if isinstance(w, (tk.Text, ttk.Combobox)):
            return
        box = self._cv.bbox("all")
        if not box or box[3] <= self._cv.winfo_height() + 1:
            return
        self._cv.yview_scroll(-1 if e.delta > 0 else 1, "units")


class SteamDialog(tk.Toplevel):
    """Steam 风格模态框：自绘标题栏（可拖动）+ 分隔线 + 底部按钮区。"""

    def __init__(self, master, title, width=600, accent=UI_WARN, icon="!", closable=True):
        owner = getattr(master, "root", master)
        super().__init__(owner)
        self.app = master
        self.result = None
        self._dlg_w = width
        self._accent = accent
        self.overrideredirect(True)
        self.configure(bg=UI_BG_MID, highlightthickness=1, highlightbackground="#3d5878")
        self.transient(owner)

        head = tk.Frame(self, bg=UI_BG_MID)
        head.pack(fill=tk.X)

        cv = tk.Canvas(head, width=34, height=34, bg=UI_BG_MID, highlightthickness=0)
        cv.pack(side=tk.LEFT, padx=(18, 12), pady=15)
        cv.create_oval(2, 2, 32, 32, fill=self._mix(accent, UI_BG_MID, .15), outline="")
        cv.create_text(17, 17, text=icon, fill=accent, font=("Arial", 12, "bold"))

        tk.Label(head, text=title, bg=UI_BG_MID, fg="#ffffff",
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)

        if closable:
            cl = tk.Label(head, text="✕", bg=UI_BG_MID, fg=UI_TEXT_DIM,
                          font=("Arial", 10), cursor="hand2")
            cl.pack(side=tk.RIGHT, padx=(0, 16))
            cl.bind("<Button-1>", lambda e: self.cancel())
            cl.bind("<Enter>", lambda e: cl.configure(fg="#ffffff"))
            cl.bind("<Leave>", lambda e: cl.configure(fg=UI_TEXT_DIM))

        tk.Frame(self, bg=UI_BORDER, height=1).pack(fill=tk.X)

        self.body = tk.Frame(self, bg=UI_BG_MID)
        self.body.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        tk.Frame(self, bg=UI_BORDER, height=1).pack(fill=tk.X)
        self.foot = tk.Frame(self, bg="#161c26")
        self.foot.pack(fill=tk.X)
        self._foot_inner = tk.Frame(self.foot, bg="#161c26")
        self._foot_inner.pack(fill=tk.X, padx=16, pady=(10, 12))

        self._drag_targets = [head]
        self._bind_drag(head)

    @staticmethod
    def _mix(c1, c2, ratio):
        """把 c1 以 ratio 比例混入 c2（模拟半透明色块）。"""
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] * ratio + b[i] * (1 - ratio)) for i in range(3))

    def _bind_drag(self, w):
        w.bind("<Button-1>", self._drag_press)
        w.bind("<B1-Motion>", self._drag_move)
        for ch in w.winfo_children():
            if isinstance(ch, tk.Label):
                ch.bind("<Button-1>", self._drag_press)
                ch.bind("<B1-Motion>", self._drag_move)

    def _drag_press(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def cancel(self):
        self.result = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def show(self):
        self.update_idletasks()
        w = max(self._dlg_w, self.winfo_reqwidth())
        h = max(self.winfo_reqheight(), 120)
        log.debug("[dialog] req=%dx%d -> %dx%d", self.winfo_reqwidth(), self.winfo_reqheight(), w, h)
        parent = getattr(self.app, "root", self.app)
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_force()
        return self


class DropdownMenu(tk.Toplevel):
    """自绘下拉菜单（支持每项「标题 + 说明」两行，向上展开）。"""

    def __init__(self, master, items, width=340):
        super().__init__(master)
        self.overrideredirect(True)
        self.configure(bg="#3d5878")
        self.attributes("-topmost", True)
        inner = tk.Frame(self, bg="#0e141d")
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        for i, (icon, title, sub, cb) in enumerate(items):
            if title is None:
                tk.Frame(inner, bg=UI_BORDER, height=1).pack(fill=tk.X)
                continue
            row = tk.Frame(inner, bg="#0e141d", cursor="hand2")
            row.pack(fill=tk.X)
            ic = tk.Label(row, text=icon, bg="#0e141d", fg=UI_ACCENT,
                          font=("Arial", 10), width=2, anchor="n")
            ic.pack(side=tk.LEFT, padx=(10, 4), pady=(9, 9))
            box = tk.Frame(row, bg="#0e141d")
            box.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=8)
            t = tk.Label(box, text=title, bg="#0e141d", fg="#ffffff", font=F_TXT, anchor="w")
            t.pack(fill=tk.X)
            d = tk.Label(box, text=sub, bg="#0e141d", fg=UI_TEXT_DIM, font=F_TXT_S,
                         anchor="w", justify=tk.LEFT, wraplength=width - 60)
            d.pack(fill=tk.X)

            def enter(_e, r=row, b=box, i2=ic, t2=t, d2=d):
                for w in (r, b, i2, t2, d2):
                    w.configure(bg=UI_BG_LIGHT)

            def leave(_e, r=row, b=box, i2=ic, t2=t, d2=d):
                for w in (r, b, i2, t2, d2):
                    w.configure(bg="#0e141d")

            def click(_e, f=cb):
                self.destroy()
                f()

            for w in (row, box, ic, t, d):
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
                w.bind("<Button-1>", click)

        if items:
            tk.Frame(inner, bg="#0e141d", height=4).pack(fill=tk.X)

        self._dlg_w = width
        self.bind("<FocusOut>", lambda e: self.destroy())
        self.bind("<Escape>", lambda e: self.destroy())

    def popup_above(self, widget):
        self.update_idletasks()
        h = self.winfo_reqheight()
        x = widget.winfo_rootx() + widget.winfo_width() - self._dlg_w
        y = widget.winfo_rooty() - h - 6
        self.geometry("%dx%d+%d+%d" % (self._dlg_w, h, x, max(0, y)))
        self.deiconify()
        self.lift()
        self.focus_force()


class MissingDialog(SteamDialog):
    """未检测到 SteamCMD 时的引导弹框。"""

    def __init__(self, master, checked_paths):
        super().__init__(master, tr("未检测到 SteamCMD"), width=600, accent=UI_WARN, icon="!")
        body = self.body
        wrap = 552

        tk.Label(body, text=tr("当前程序所在目录及其上级目录中，"), bg=UI_BG_MID, fg=UI_TEXT,
                 font=F_TXT, anchor="w").pack(fill=tk.X)
        tk.Label(body, text=tr("均未找到 steamcmd.exe。"), bg=UI_BG_MID, fg=UI_WARN,
                 font=F_TXT, anchor="w").pack(fill=tk.X, pady=(0, 10))

        box = tk.Frame(body, bg=UI_ENTRY, highlightthickness=1, highlightbackground=UI_BORDER)
        box.pack(fill=tk.X)
        tk.Label(box, text=tr("已检查路径"), bg=UI_ENTRY, fg=UI_TEXT_DIM, font=F_TXT_S,
                 anchor="w").pack(fill=tk.X, padx=10, pady=(8, 3))
        for p in (checked_paths or [])[:4]:
            tk.Label(box, text="✕  " + str(p), bg=UI_ENTRY, fg="#9aa5ad", font=F_MONO,
                     anchor="w").pack(fill=tk.X, padx=10)
        tk.Frame(box, bg=UI_ENTRY, height=8).pack(fill=tk.X)

        # 黄色提示条（带「自动复制过去」入口）
        hint = tk.Frame(body, bg=UI_WARN_BG)
        hint.pack(fill=tk.X, pady=(12, 0))
        tk.Frame(hint, bg=UI_WARN, width=3).pack(side=tk.LEFT, fill=tk.Y)
        ui_btn(hint, tr("自动复制过去…"), "secondary", self._auto_copy).pack(
            side=tk.RIGHT, padx=(8, 10), pady=10)
        tk.Label(hint, text=tr("如果你已经下载并安装过 SteamCMD，只需把本程序（steamcmd_gui.py）"
                            "放到 steamcmd.exe 所在目录，再重新打开即可。"),
                 bg=UI_WARN_BG, fg="#cfd8de", font=F_TXT_S, justify=tk.LEFT,
                 wraplength=380, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True,
                                                  padx=10, pady=9)

        tk.Label(body, text=tr("若尚未安装：SteamCMD 是 Valve 官方提供的命令行工具，"
                            "本程序需要它来完成 Steam 登录与内容上传。"),
                 bg=UI_BG_MID, fg=UI_TEXT, font=F_TXT, justify=tk.LEFT, wraplength=wrap,
                 anchor="w").pack(fill=tk.X, pady=(14, 2))
        tk.Label(body, text=tr("是否现在下载并安装？"), bg=UI_BG_MID, fg="#ffffff",
                 font=F_BOLD, anchor="w").pack(fill=tk.X, pady=(0, 10))

        urow = tk.Frame(body, bg=UI_BG_MID)
        urow.pack(fill=tk.X)
        self._url = tk.Entry(urow, bg=UI_ENTRY, fg=UI_ACCENT, font=F_MONO, relief=tk.FLAT,
                             insertbackground=UI_ACCENT, readonlybackground=UI_ENTRY,
                             highlightthickness=1, highlightbackground=UI_BORDER)
        self._url.insert(0, STEAMCMD_ZIP_URL)
        self._url.configure(state="readonly")
        self._url.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self._copy_btn = ui_btn(urow, tr("复制"), "secondary", self._copy_url)
        self._copy_btn.pack(side=tk.LEFT, padx=(8, 0))

        ui_link(body, tr("SteamCMD 官方使用文档 ↗"), STEAMCMD_DOC_URL).pack(anchor="w", pady=(9, 0))
        tk.Label(body, text=tr("若网络受限导致下载失败，可手动用浏览器打开上面的地址，"
                            "解压后把 steamcmd.exe 放到本程序同目录。"),
                 bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, justify=tk.LEFT, wraplength=wrap,
                 anchor="w").pack(fill=tk.X, pady=(10, 0))

        # footer：最右是「下载 ▼」，右边距留白
        split = tk.Frame(self._foot_inner, bg="#161c26")
        split.pack(side=tk.RIGHT)
        ui_btn(split, tr("下载"), "primary", self._do_download).pack(side=tk.LEFT)
        self._arrow = RoundButton(split, text="\u25be", command=self._open_menu,
                                  kind="primary", minw=26, height=30, font=("Arial", 8))
        self._arrow.pack(side=tk.LEFT, padx=(2, 0))
        ui_btn(self._foot_inner, tr("稍后再说"), "ghost", self.cancel).pack(side=tk.RIGHT, padx=(0, 10))

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(STEAMCMD_ZIP_URL)
        self._copy_btn.configure(text=tr("已复制"))
        self.after(1200, lambda: self._copy_btn.configure(text=tr("复制")))

    def _open_menu(self):
        items = [
            ("↓", tr("下载并安装到指定目录…"), tr("选择目录 → 自动下载 / 解压 / 初始化，并把本程序复制过去"),
             self._do_download),
            ("▦", tr("仅下载 zip 文件…"), tr("保存到磁盘，稍后自己解压使用"), self._zip_only),
            (None, None, None, None),
            ("↗", tr("在浏览器打开官方下载页"), "Valve Developer Community",
             lambda: __import__("webbrowser").open(STEAMCMD_DOC_URL)),
        ]
        DropdownMenu(self, items).popup_above(self._arrow)

    def _auto_copy(self):
        self.cancel()
        self.app.ask_copy_to_existing()

    def _do_download(self):
        self.cancel()
        self.app.ask_install_dir()

    def _zip_only(self):
        self.cancel()
        self.app.download_zip_only()


class PickDirDialog(SteamDialog):
    """选择 SteamCMD 安装目录。"""

    def __init__(self, master, default_dir=""):
        super().__init__(master, tr("选择 SteamCMD 安装目录"), width=520, accent=UI_ACCENT, icon="↓")
        body = self.body
        self.result = None

        tk.Label(body, text=tr("SteamCMD 将解压到该目录；安装成功后，本程序（steamcmd_gui.py）"
                            "也会被复制到这里。"),
                 bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, justify=tk.LEFT, wraplength=472,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))

        row = tk.Frame(body, bg=UI_BG_MID)
        row.pack(fill=tk.X)
        self._dir = tk.Entry(row, bg=UI_ENTRY, fg="#ffffff", font=F_MONO, relief=tk.FLAT,
                             insertbackground=UI_ACCENT, highlightthickness=1,
                             highlightbackground=UI_BORDER)
        self._dir.insert(0, default_dir or os.path.join(str(Path.home()), "SteamCMD"))
        self._dir.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        ui_btn(row, tr("浏览…"), "secondary", self._browse).pack(side=tk.LEFT, padx=(8, 0))

        chips = tk.Frame(body, bg=UI_BG_MID)
        chips.pack(fill=tk.X, pady=(9, 0))
        _home_sc = os.path.join(str(Path.home()), "SteamCMD")
        _local_sc = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SteamCMD")
        _drive_sc = "D:" + chr(92) + "SteamCMD"
        for _label, _full in [(tr("默认位置"), _home_sc),
                              (_drive_sc, _drive_sc),
                              (tr("本地应用数据"), _local_sc)]:
            if not _full:
                continue
            ui_btn(chips, _label, "ghost", lambda x=_full: self._set_dir(x)).pack(
                side=tk.LEFT, padx=(0, 6))

        meta = tk.Frame(body, bg=UI_BG_MID)
        meta.pack(fill=tk.X, pady=(12, 0))
        for txt in [tr("安装包 774 KB"), tr("解压后 约 3 MB"), tr("首次初始化 约 300 MB")]:
            tk.Label(meta, text=txt, bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S).pack(
                side=tk.LEFT, padx=(0, 14))

        box = tk.Frame(body, bg=UI_WARN_BG)
        box.pack(fill=tk.X, pady=(12, 0))
        tk.Frame(box, bg=UI_WARN, width=3).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(box, text=tr("目录已有同名文件时会被覆盖，其它文件不受影响。"
                           "首次初始化需要联网，耗时通常 1–5 分钟。"),
                 bg=UI_WARN_BG, fg="#cfd8de", font=F_TXT_S, justify=tk.LEFT, wraplength=460,
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=8)

        ui_btn(self._foot_inner, tr("开始下载并安装"), "primary", self._ok).pack(side=tk.RIGHT)
        ui_btn(self._foot_inner, tr("取消"), "ghost", self.cancel).pack(side=tk.RIGHT, padx=(0, 10))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.cancel())

    def _set_dir(self, p):
        self._dir.delete(0, tk.END)
        self._dir.insert(0, p)

    def _browse(self):
        d = filedialog.askdirectory(title=tr("选择安装目录"), parent=self)
        if d:
            self._set_dir(d)

    def _ok(self):
        d = self._dir.get().strip().strip('"')
        if not d:
            return
        self.result = d
        self.cancel()
        self.result = d

    def cancel(self):
        r = getattr(self, "result", None)
        super().cancel()
        self.result = r


class CopyDialog(SteamDialog):
    """把本程序复制到已有 SteamCMD 目录。"""

    def __init__(self, master, default_dir=""):
        super().__init__(master, tr("把本程序复制到 SteamCMD 目录"),
                         width=520, accent=UI_ACCENT, icon="⇄")
        body = self.body
        self.result = None

        tk.Label(body, text=tr("选择 steamcmd.exe 所在的文件夹，本程序会被复制过去，"
                            "之后从那里启动就能找到 SteamCMD。"),
                 bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, justify=tk.LEFT, wraplength=472,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))

        row = tk.Frame(body, bg=UI_BG_MID)
        row.pack(fill=tk.X)
        self._dir = tk.Entry(row, bg=UI_ENTRY, fg="#ffffff", font=F_MONO, relief=tk.FLAT,
                             insertbackground=UI_ACCENT, highlightthickness=1,
                             highlightbackground=UI_BORDER)
        self._dir.insert(0, default_dir or "")
        self._dir.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        ui_btn(row, tr("浏览…"), "secondary", self._browse).pack(side=tk.LEFT, padx=(8, 0))
        self._dir.bind("<KeyRelease>", lambda e: self._verify())

        self._verify_lb = tk.Label(body, text="", bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S,
                                   anchor="w", justify=tk.LEFT)
        self._verify_lb.pack(fill=tk.X, pady=(8, 0))

        self._with_cfg = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(body, text=tr("同时带上现有配置 steamcmd_gui.cfg（保留账号与上传设置）"),
                            variable=self._with_cfg, bg=UI_BG_MID, fg=UI_TEXT, font=F_TXT_S,
                            selectcolor=UI_ENTRY, activebackground=UI_BG_MID,
                            activeforeground="#ffffff", anchor="w", cursor="hand2",
                            bd=0, highlightthickness=0)
        cb.pack(fill=tk.X, pady=(10, 0))

        box = tk.Frame(body, bg=UI_ENTRY, highlightthickness=1, highlightbackground=UI_BORDER)
        box.pack(fill=tk.X, pady=(12, 0))
        tk.Label(box, text=tr("将写入"), bg=UI_ENTRY, fg=UI_TEXT_DIM, font=F_TXT_S,
                 anchor="w").pack(fill=tk.X, padx=10, pady=(8, 3))
        self._write_lb = tk.Label(box, text="—", bg=UI_ENTRY, fg="#9aa5ad", font=F_MONO,
                                  anchor="w", justify=tk.LEFT, wraplength=460)
        self._write_lb.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._ok_btn = ui_btn(self._foot_inner, tr("复制过去"), "primary", self._ok)
        self._ok_btn.pack(side=tk.RIGHT)
        ui_btn(self._foot_inner, tr("取消"), "ghost", self.cancel).pack(side=tk.RIGHT, padx=(0, 10))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.cancel())

        if default_dir:
            self._verify()

    def _browse(self):
        d = filedialog.askdirectory(title=tr("选择 steamcmd.exe 所在目录"), parent=self)
        if d:
            self._dir.delete(0, tk.END)
            self._dir.insert(0, d)
            self._verify()

    def _verify(self):
        d = self._dir.get().strip().strip('"')
        if not d:
            self._verify_lb.configure(text=tr("请选择 steamcmd.exe 所在目录"), fg=UI_TEXT_DIM)
            self._write_lb.configure(text="—")
            self._ok_btn.configure(state=tk.DISABLED)
            return
        exe = os.path.join(d, "steamcmd.exe")
        name = os.path.basename(SteamCmdInstaller.self_path())
        if os.path.isfile(exe):
            self._verify_lb.configure(text=tr("✓  已找到 ") + exe, fg=UI_GREEN)
            self._ok_btn.configure(state=tk.NORMAL)
        else:
            self._verify_lb.configure(text=tr("✕  该目录下没有 steamcmd.exe，请重新选择"), fg=UI_WARN)
            self._ok_btn.configure(state=tk.DISABLED)
        self._write_lb.configure(text="＋  " + os.path.join(d, name))

    def _ok(self):
        d = self._dir.get().strip().strip('"')
        if not d or not os.path.isfile(os.path.join(d, "steamcmd.exe")):
            return
        self.result = (d, bool(self._with_cfg.get()))
        self.destroy()

    def destroy(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        super().destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class InstallPanel(tk.Frame):
    """嵌入主页的安装进度卡片。"""

    def __init__(self, parent, app):
        super().__init__(parent, bg=UI_BG_MID, highlightthickness=1, highlightbackground=UI_BORDER)
        self.app = app
        self.STEPS = [tr("下载"), tr("解压"), tr("首次初始化"), tr("复制本程序")]
        self.cancel_evt = threading.Event()
        self._steps = [0, 0, 0, 0]
        self._target = ""
        self._done_paths = []

        head = tk.Frame(self, bg=UI_BG_MID)
        head.pack(fill=tk.X, padx=14, pady=(12, 0))
        self._ic = tk.Canvas(head, width=34, height=34, bg=UI_BG_MID, highlightthickness=0)
        self._ic.pack(side=tk.LEFT, padx=(0, 10))
        hb = tk.Frame(head, bg=UI_BG_MID)
        hb.pack(side=tk.LEFT)
        self._title = tk.Label(hb, text=tr("正在安装 SteamCMD"), bg=UI_BG_MID, fg="#ffffff",
                               font=F_BOLD, anchor="w")
        self._title.pack(anchor="w")
        self._sub = tk.Label(hb, text="", bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w")
        self._sub.pack(anchor="w")
        self._cancel_btn = ui_btn(head, tr("取消"), "ghost", self._on_cancel)
        self._cancel_btn.pack(side=tk.RIGHT)

        self._steps_cv = tk.Canvas(self, height=50, bg=UI_BG_MID, highlightthickness=0)
        self._steps_cv.pack(fill=tk.X, padx=14, pady=(10, 0))
        self._steps_cv.bind("<Configure>", lambda e: self._draw_steps())

        prow = tk.Frame(self, bg=UI_BG_MID)
        prow.pack(fill=tk.X, padx=14, pady=(8, 0))
        self._ptext_l = tk.Label(prow, text=tr("准备中…"), bg=UI_BG_MID, fg=UI_TEXT_DIM,
                                 font=F_TXT_S, anchor="w")
        self._ptext_l.pack(side=tk.LEFT)
        self._ptext_r = tk.Label(prow, text=tr("0 / 4 步"), bg=UI_BG_MID, fg=UI_TEXT_DIM,
                                 font=F_TXT_S, anchor="e")
        self._ptext_r.pack(side=tk.RIGHT)

        self._pbar = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self._pbar.pack(fill=tk.X, padx=14, pady=(4, 0))

        self._log = tk.Text(self, height=7, bg=UI_LOG_BG, fg="#c9d1d9", font=F_MONO,
                            relief=tk.FLAT, bd=0, padx=8, pady=5, state=tk.DISABLED,
                            wrap=tk.WORD, highlightthickness=1, highlightbackground=UI_BORDER)
        self._log.pack(fill=tk.BOTH, expand=True, padx=14, pady=(10, 0))
        self._log.tag_configure("dim", foreground="#5a6b7a")
        self._log.tag_configure("ok", foreground=UI_GREEN)
        self._log.tag_configure("warn", foreground=UI_WARN)

        foot = tk.Frame(self, bg=UI_BG_MID)
        foot.pack(fill=tk.X, padx=14, pady=(8, 12))
        self._foot_lb = tk.Label(foot, text=tr("安装过程中请勿关闭窗口"), bg=UI_BG_MID,
                                 fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w")
        self._foot_lb.pack(side=tk.LEFT)
        self._restart_btn = ui_btn(foot, tr("立即在新目录重启"), "primary", self._on_restart)
        self._later_btn = ui_btn(foot, tr("稍后"), "ghost", self._on_later)

    # ── 绘制 ──
    def _draw_icon(self, color, symbol):
        c = self._ic
        c.delete("all")
        c.create_oval(2, 2, 32, 32, fill=SteamDialog._mix(color, UI_BG_MID, .2), outline="")
        c.create_text(17, 17, text=symbol, fill=color, font=("Arial", 12, "bold"))

    def _draw_steps(self):
        c = self._steps_cv
        c.delete("all")
        w = c.winfo_width()
        if w < 80:
            return
        n = len(self.STEPS)
        margin = 42
        gap = (w - margin * 2) / (n - 1)
        y = 16
        for i in range(n - 1):
            x1, x2 = margin + i * gap, margin + (i + 1) * gap
            c.create_line(x1, y, x2, y,
                          fill=(UI_GREEN if self._steps[i] == 2 else "#33506e"), width=2)
        for i, name in enumerate(self.STEPS):
            cx = margin + i * gap
            st = self._steps[i]
            if st == 2:
                fill = SteamDialog._mix(UI_GREEN, UI_BG_MID, .25)
                outline, txt, tf = UI_GREEN, "✓", UI_GREEN
            elif st == 1:
                fill = SteamDialog._mix(UI_ACCENT, UI_BG_MID, .25)
                outline, txt, tf = UI_ACCENT, str(i + 1), UI_ACCENT
            else:
                fill, outline, txt, tf = UI_ENTRY, UI_BORDER, str(i + 1), UI_TEXT_DIM
            c.create_oval(cx - 11, y - 11, cx + 11, y + 11, fill=fill, outline=outline, width=1)
            c.create_text(cx, y, text=txt, fill=tf, font=("Arial", 8, "bold"))
            c.create_text(cx, y + 25, text=name,
                          fill=(UI_TEXT if st else UI_TEXT_DIM), font=F_TXT_S)

    # ── 操作 ──
    def start(self, target_dir):
        self._target = target_dir
        self._steps = [0, 0, 0, 0]
        self._done_paths = []
        self.cancel_evt.clear()
        self._draw_icon(UI_ACCENT, "↓")
        self._title.configure(text=tr("正在安装 SteamCMD"))
        self._sub.configure(text=tr("目标目录  ") + target_dir)
        self._ptext_l.configure(text=tr("准备中…"))
        self._ptext_r.configure(text=tr("0 / 4 步"))
        self._pbar.stop()
        self._pbar.configure(mode="determinate", value=0)
        self._foot_lb.configure(text=tr("安装过程中请勿关闭窗口"), fg=UI_TEXT_DIM)
        self._cancel_btn.configure(state=tk.NORMAL)
        if not self._cancel_btn.winfo_ismapped():
            self._cancel_btn.pack(side=tk.RIGHT)
        self._restart_btn.pack_forget()
        self._later_btn.pack_forget()
        self._log_clear()
        self._draw_steps()

    def set_step(self, index, state):
        if 0 <= index < len(self._steps):
            self._steps[index] = state
        done = sum(1 for s in self._steps if s == 2)
        self._ptext_r.configure(text=tr("%d / 4 步") % done)
        self._draw_steps()

    def set_progress(self, done, total, label=None):
        if total > 0:
            if str(self._pbar.cget("mode")) != "determinate":
                self._pbar.stop()
                self._pbar.configure(mode="determinate")
            self._pbar.configure(value=int(done * 1000 / total))
            if label is None:
                label = "%.1f MB / %.1f MB" % (done / 1048576, total / 1048576)
        else:
            # 无精确百分比（首次初始化）：用不确定动画表示仍在进行
            if str(self._pbar.cget("mode")) != "indeterminate":
                self._pbar.configure(mode="indeterminate")
                self._pbar.start(18)
            label = label or "%.1f MB" % (done / 1048576)
        if label:
            self._ptext_l.configure(text=label)

    def log(self, text, tag=None):
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n", tag or ())
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _log_clear(self):
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)

    def finish_success(self, paths, target, restartable=True):
        self._done_paths = paths
        self._target = target
        self._steps = [2, 2, 2, 2]
        self._draw_icon(UI_GREEN, "✓")
        self._title.configure(text=tr("SteamCMD 安装完成"))
        self._sub.configure(text=tr("已就绪，可以登录 Steam 了"))
        self._pbar.stop()
        self._pbar.configure(mode="determinate", value=1000)
        self._ptext_l.configure(text=tr("完成"))
        self._ptext_r.configure(text=tr("4 / 4 步"))
        self._foot_lb.configure(text=tr("也可以稍后手动双击目标目录下的 ") +
                                     os.path.basename(SteamCmdInstaller.self_path()) + tr(" 启动"),
                                fg=UI_TEXT_DIM)
        self._cancel_btn.pack_forget()
        self._draw_steps()
        if restartable:
            self._later_btn.pack(side=tk.RIGHT, padx=(0, 8))
            self._restart_btn.pack(side=tk.RIGHT)

    def finish_error(self, msg):
        self._draw_icon(UI_WARN, "!")
        self._title.configure(text=tr("安装未完成"))
        self._sub.configure(text=msg)
        self._pbar.configure(value=0)
        self._pbar.stop()
        self._pbar.configure(mode="determinate", value=0)
        self._ptext_l.configure(text=tr("已中止"))
        self._foot_lb.configure(text=tr("可以重试，或手动下载 zip 解压后把 steamcmd.exe 放到本程序目录"),
                                fg=UI_TEXT_DIM)
        self._cancel_btn.pack_forget()
        self._later_btn.pack(side=tk.RIGHT)

    def _on_cancel(self):
        self.cancel_evt.set()
        self._ptext_l.configure(text=tr("正在取消…"))
        self._cancel_btn.configure(state=tk.DISABLED)

    def _on_restart(self):
        try:
            SteamCmdInstaller.relaunch(self._target)
        except Exception as e:
            messagebox.showerror(tr("启动失败"), tr("无法在新目录启动：\n%s") % e, parent=self.app.root)
            return
        self.app.root.destroy()

    def _on_later(self):
        self.pack_forget()
        self.app.on_install_panel_closed()


class DoneDialog(SteamDialog):
    """操作完成提示（复制成功 / 已就绪）。"""

    def __init__(self, master, title, subtitle, rows, action_text=None, hint=None):
        super().__init__(master, title, width=520, accent=UI_GREEN, icon="✓")
        self.result = False
        body = self.body

        tk.Label(body, text=subtitle, bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S,
                 anchor="w").pack(fill=tk.X, pady=(0, 12))

        box = tk.Frame(body, bg=UI_ENTRY, highlightthickness=1, highlightbackground=UI_BORDER)
        box.pack(fill=tk.X)
        for k, v, tag in rows:
            r = tk.Frame(box, bg=UI_ENTRY)
            r.pack(fill=tk.X, padx=10, pady=3)
            tk.Label(r, text=k, bg=UI_ENTRY, fg=UI_TEXT_DIM, font=F_TXT_S,
                     width=9, anchor="w").pack(side=tk.LEFT)
            tk.Label(r, text=v, bg=UI_ENTRY, fg="#ffffff", font=F_MONO,
                     anchor="w", justify=tk.LEFT, wraplength=330).pack(side=tk.LEFT, fill=tk.X)
            if tag:
                tk.Label(r, text=tag, bg=UI_GREEN_BG, fg=UI_GREEN, font=("Microsoft YaHei", 7),
                         padx=5).pack(side=tk.LEFT, padx=(6, 0))

        if hint:
            hbg = self._mix(self._accent, UI_BG_MID, .12)
            hb = tk.Frame(body, bg=hbg)
            hb.pack(fill=tk.X, pady=(12, 0))
            tk.Frame(hb, bg=self._accent, width=3).pack(side=tk.LEFT, fill=tk.Y)
            tk.Label(hb, text=hint, bg=hbg, fg="#cfd8de", font=F_TXT_S, justify=tk.LEFT,
                     wraplength=460, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True,
                                                     padx=10, pady=8)

        if action_text:
            ui_btn(self._foot_inner, action_text, "primary", self._ok).pack(side=tk.RIGHT)
        ui_btn(self._foot_inner, tr("稍后") if action_text else tr("关闭"), "ghost",
               self.cancel).pack(side=tk.RIGHT, padx=(0, 10))

    def _ok(self):
        self.result = True
        self.destroy()


# ── GUI ──────────────────────────────────
class App:
    def __init__(self):
        self.cfg = Config(); self.steamcmd = None
        set_lang(self.cfg.lang)
        self.running = False
        self._update_available = None
        self.install_panel = None
        self._install_busy = False
        self.root = tk.Tk()
        self.root.title("SteamCMD GUI"); self.root.geometry("960x700")
        self.root.minsize(880, 600)
        self._set_icon()
        self._setup_theme()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._startup)
        self.root.after(5000, self._check_update_startup)

    def _set_icon(self):
        """标题栏图标。

        exe 的图标是打在可执行文件资源里的，tkinter 看不到，得另给一份图片；
        onefile 打包时这个 ico 会被解到 _MEIPASS 临时目录里，所以两个位置都找一下。
        找不到就算了 —— 图标不值得让程序起不来。
        """
        base = getattr(sys, "_MEIPASS", None) or str(APP_DIR)
        for name in ("steamcmd_gui.ico",):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                try:
                    self.root.iconbitmap(default=p)
                    return
                except tk.TclError:
                    pass

    def _setup_theme(self):
        # ── ttk 暗色主题（Steam 配色）──
        self.style = ttk.Style()
        if 'clam' in self.style.theme_names():
            self.style.theme_use('clam')
        s = self.style
        C_BG_DARK   = "#171a21"
        C_BG_MID    = "#1b2838"
        C_BG_LIGHT  = "#2a475e"
        C_BORDER    = "#2a3f5a"
        C_ACCENT    = "#66c0f4"
        C_ACCENT_BG = "#417a9b"
        C_ACCENT_HOVER = "#4ba8d6"
        C_GREEN     = "#4a7513"
        C_GREEN_HOVER  = "#5c9318"
        C_TEXT      = "#c7d5e0"
        C_TEXT_DIM  = "#8f98a0"
        C_ENTRY_BG  = "#11151c"
        C_FONT      = ("Microsoft YaHei", 10)
        C_FONT_MONO = ("Consolas", 9)

        s.configure('.', background=C_BG_MID, foreground=C_TEXT,
                    bordercolor=C_BORDER, lightcolor=C_BG_LIGHT,
                    darkcolor=C_BG_DARK, font=C_FONT, troughcolor=C_BG_DARK)

        s.configure('TButton', background=C_BG_LIGHT, foreground=C_TEXT,
                    bordercolor=C_BORDER, lightcolor=C_BG_LIGHT, darkcolor=C_BG_DARK,
                    relief='raised', padding=(8, 4), font=C_FONT)
        s.map('TButton',
              background=[('active', C_ACCENT_HOVER), ('pressed', C_BG_MID)],
              foreground=[('active', '#ffffff')])

        s.configure('Accent.TButton', background=C_ACCENT_BG, foreground='#ffffff',
                    bordercolor=C_BORDER, relief='raised', padding=(8, 4), font=C_FONT)
        s.map('Accent.TButton',
              background=[('active', C_ACCENT_HOVER), ('pressed', C_BG_LIGHT)],
              foreground=[('active', '#ffffff')])

        s.configure('Green.TButton', background=C_GREEN, foreground='#ffffff',
                    bordercolor=C_BORDER, relief='raised', padding=(8, 4), font=C_FONT)
        s.map('Green.TButton',
              background=[('active', C_GREEN_HOVER), ('pressed', C_BG_LIGHT)],
              foreground=[('active', '#ffffff')])

        s.configure('TEntry', fieldbackground=C_ENTRY_BG, foreground='#ffffff',
                    bordercolor=C_BORDER, lightcolor=C_ENTRY_BG, darkcolor=C_ENTRY_BG,
                    padding=5, font=C_FONT)
        s.map('TEntry', bordercolor=[('focus', C_ACCENT)])

        s.configure('TCombobox', fieldbackground=C_ENTRY_BG, foreground='#ffffff',
                    background=C_BG_LIGHT, bordercolor=C_BORDER, padding=5, font=C_FONT)
        s.map('TCombobox', fieldbackground=[('focus', C_ENTRY_BG)],
              foreground=[('disabled', C_TEXT_DIM)])

        s.configure('TNotebook', background=C_BG_DARK, bordercolor=C_BORDER)
        s.configure('TNotebook.Tab', background=C_BG_MID, foreground=C_TEXT_DIM,
                    padding=(16, 8), bordercolor=C_BORDER, font=C_FONT)
        s.map('TNotebook.Tab',
              background=[('selected', C_BG_LIGHT)],
              foreground=[('selected', C_ACCENT)])

        s.configure('TProgressbar', background=C_ACCENT, troughcolor=C_ENTRY_BG,
                    bordercolor=C_BORDER, thickness=10)

        s.configure('TLabelframe', background=C_BG_MID, foreground=C_TEXT, bordercolor=C_BORDER)
        s.configure('TLabelframe.Label', background=C_BG_MID, foreground='#ffffff',
                    font=("Microsoft YaHei", 10, "bold"))

        s.configure('TRadiobutton', background=C_BG_MID, foreground=C_TEXT, font=C_FONT)
        s.map('TRadiobutton', background=[('active', C_BG_MID)])
        s.configure('TCheckbutton', background=C_BG_MID, foreground=C_TEXT, font=C_FONT)
        s.map('TCheckbutton', background=[('active', C_BG_MID)])

        s.configure('Treeview', background=C_BG_MID, foreground=C_TEXT,
                    fieldbackground=C_BG_MID, bordercolor=C_BORDER, font=C_FONT)
        s.map('Treeview', background=[('selected', C_BG_LIGHT)],
              foreground=[('selected', '#ffffff')])
        s.configure('Treeview.Heading', background=C_BG_LIGHT, foreground=C_TEXT,
                    bordercolor=C_BORDER, font=("Microsoft YaHei", 9, "bold"))

        s.configure('TScrollbar', background=C_BG_LIGHT, troughcolor=C_BG_DARK,
                    bordercolor=C_BORDER, arrowcolor=C_TEXT)

        # ── tk 控件默认（option database）──
        r = self.root
        r.option_add('*Font', C_FONT)
        r.option_add('*background', C_BG_MID)
        r.option_add('*foreground', C_TEXT)
        r.option_add('*TkFrame.background', C_BG_MID)
        r.option_add('*Toplevel.background', C_BG_MID)
        r.option_add('*Text.background', '#0d1117')
        r.option_add('*Text.foreground', C_TEXT)
        r.option_add('*Text.font', C_FONT_MONO)
        r.option_add('*Text.selectBackground', C_BG_LIGHT)
        r.option_add('*Text.selectForeground', '#ffffff')
        r.option_add('*Listbox.background', C_BG_MID)
        r.option_add('*Listbox.foreground', C_TEXT)
        r.option_add('*Listbox.font', ("Consolas", 10))
        r.option_add('*Listbox.selectBackground', C_BG_LIGHT)
        r.option_add('*Listbox.selectForeground', '#ffffff')
        r.option_add('*Button.background', C_BG_LIGHT)
        r.option_add('*Button.foreground', C_TEXT)
        r.option_add('*Button.activeBackground', C_ACCENT_HOVER)
        r.option_add('*Button.activeForeground', '#ffffff')
        r.option_add('*Button.relief', 'flat')
        r.option_add('*Button.borderWidth', 0)
        r.option_add('*Button.highlightThickness', 0)
        r.configure(bg=C_BG_DARK)

    def _build_ui(self):
        # 状态栏先占位，避免被主体挤压
        status_frame = tk.Frame(self.root, bg="#0f1319", relief=tk.FLAT, bd=0)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status = tk.Label(status_frame, text=tr("就绪"), anchor=tk.W, padx=10,
                               bg="#0f1319", fg="#8f98a0", font=("Microsoft YaHei", 8))
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.login_indicator = tk.Label(status_frame, text=tr("未登录"), fg="#7d8a96", bg="#0f1319",
                                        anchor=tk.E, padx=10, font=("Microsoft YaHei", 8))
        self.login_indicator.pack(side=tk.RIGHT)

        shell = tk.Frame(self.root, bg=UI_BG_DARK)
        shell.pack(fill=tk.BOTH, expand=True)

        # ── 左侧栏 ──
        side = tk.Frame(shell, bg="#141821", width=196)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)

        logo = tk.Frame(side, bg="#141821")
        logo.pack(fill=tk.X, padx=16, pady=(15, 12))
        tk.Label(logo, text="\u2699", bg="#141821", fg=UI_ACCENT,
                 font=("Arial", 15, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(logo, text="SteamCMD GUI", bg="#141821", fg="#ffffff",
                 font=("Arial", 11, "bold")).pack(side=tk.LEFT)

        self._nav = {}
        self._nav_enabled = {0: True, 1: False, 2: False, 3: True}
        self._active_tab = 0
        for idx, label in enumerate([tr("主页"), tr("登录"), tr("上传"), tr("设置")]):
            row = tk.Frame(side, bg="#141821", cursor="hand2")
            row.pack(fill=tk.X)
            bar = tk.Frame(row, bg="#141821", width=3)
            bar.pack(side=tk.LEFT, fill=tk.Y)
            lb = tk.Label(row, text=label, bg="#141821", fg="#7d8a96",
                          font=("Microsoft YaHei", 10), anchor="w", padx=14, pady=9)
            lb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            for w in (row, bar, lb):
                w.bind("<Button-1>", lambda e, i=idx: self.select_tab(i))
                w.bind("<Enter>", lambda e, i=idx: self._nav_hover(i, True))
                w.bind("<Leave>", lambda e, i=idx: self._nav_hover(i, False))
            self._nav[idx] = (row, bar, lb)

        tk.Frame(side, bg="#141821").pack(fill=tk.BOTH, expand=True)
        tk.Label(side, text="v" + __version__, bg="#141821", fg="#4e5b66",
                 font=("Consolas", 8)).pack(pady=(0, 10))

        # ── 右侧主体 ──
        right = tk.Frame(shell, bg=UI_BG_MID)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._update_bar = tk.Frame(right, bg=UI_BG_LIGHT, relief=tk.FLAT, bd=0)
        self._update_btn = tk.Button(self._update_bar, text=tr("\u2197 新版本可用"), bg=UI_BG_LIGHT,
            fg=UI_ACCENT, relief=tk.FLAT, activebackground=UI_ACCENT_BG,
            activeforeground="#ffffff", font=("Microsoft YaHei", 8), cursor="hand2", bd=0,
            command=self._show_update_dialog)
        self._update_btn.pack(side=tk.LEFT, padx=10, pady=3)
        self._update_bar.pack_forget()

        self._console_frame = tk.Frame(right, bg=UI_LOG_BG, highlightthickness=1,
                                       highlightbackground=UI_BORDER)
        self._console_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        self._console_frame.configure(height=142)
        self._console_frame.pack_propagate(False)
        tk.Label(self._console_frame, text=tr("控制台输出"), bg=UI_LOG_BG, fg="#5f6f7c",
                 font=("Microsoft YaHei", 8), anchor="w").pack(fill=tk.X, padx=8, pady=(4, 0))
        self.console = tk.Text(self._console_frame, height=5, font=("Consolas", 9),
            state=tk.DISABLED, bg=UI_LOG_BG, fg="#c9d1d9", relief=tk.FLAT, bd=0,
            highlightthickness=0, insertbackground="#c9d1d9")
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(2, 6))
        scb = SteamScrollbar(self._console_frame, command=self.console.yview,
                             bg=UI_LOG_BG, thumb="#33414d")
        self.console.configure(yscrollcommand=scb.set)
        scb.pack(side=tk.RIGHT, fill=tk.Y, pady=(2, 6), padx=(0, 4))

        self.page_host = tk.Frame(right, bg=UI_BG_MID)
        self.page_host.pack(fill=tk.BOTH, expand=True)

        self.tab_home = tk.Frame(self.page_host, bg=UI_BG_MID)
        self.tab_login = tk.Frame(self.page_host, bg=UI_BG_MID)
        self.tab_upload = tk.Frame(self.page_host, bg=UI_BG_MID)
        self.tab_settings = tk.Frame(self.page_host, bg=UI_BG_MID)
        self._pages = [self.tab_home, self.tab_login, self.tab_upload, self.tab_settings]

        self._build_home(); self._build_upload(); self._build_login(); self._build_settings()
        self.tab_home.pack(fill=tk.BOTH, expand=True)
        self._refresh_nav()

    # ── 多语言切换 ──
    # 文案都是建控件时写死的，改语言只能整棵树重建；重建前后把用户输入搬过去。
    def _switch_lang(self, code):
        if code == LANG:
            return
        if self.running or self._install_busy:
            messagebox.showwarning(tr("提示"), tr("安装或上传正在进行，等它结束后再切换语言"),
                                   parent=self.root)
            return
        self.cfg.lang = code
        set_lang(code)
        self._rebuild_ui()

    def _snapshot_inputs(self):
        """记下重建前用户已经填/已经选的东西。"""
        snap = {}
        for name in ("ul_appid", "ul_content", "ul_preview", "ul_tags",
                     "ul_desc", "ul_build_out", "ul_depot",
                     "login_user", "login_pass"):
            w = getattr(self, name, None)
            if isinstance(w, (tk.Entry, ttk.Entry)):
                try:
                    snap[name] = w.get()
                except tk.TclError:
                    pass
        t = getattr(self, "ul_note", None)
        if isinstance(t, tk.Text):
            # end-1c：tk.Text 结尾总带一个隐式换行，切多次语言会越堆越多
            snap["ul_note"] = t.get("1.0", "end-1c")
        return snap

    def _rebuild_ui(self):
        tab = self._active_tab
        logged = getattr(self, "login_user_val", "")
        try:
            log_text = self.console.get("1.0", tk.END).rstrip("\n")
        except Exception:
            log_text = ""
        snap = self._snapshot_inputs()
        mode = self.ul_mode.get() if hasattr(self, "ul_mode") else "workshop"
        # 导航与主页按钮的启用态是「运行时状态」，重建会退回初始值（登录页直接变灰），
        # 所以先存下来，建完再盖回去。
        nav = dict(self._nav_enabled)
        btns = [b._enabled for b in getattr(self, "home_btns", [])]
        # 登录进行到一半（尤其正等着输入验证码）时切语言，不能把输入框和已输的内容弄丢
        l_status = self.login_status.get()
        l_guard = self.login_guard.get()
        l_guard_on = bool(self.login_guard_frame.winfo_manager())
        l_btn_on, l_btn_txt = self.login_btn._enabled, self.login_btn._text

        for w in self.root.winfo_children():
            w.destroy()
        self.install_panel = None
        self._build_ui()

        self._nav_enabled = nav
        for b, on in zip(getattr(self, "home_btns", []), btns):
            ui_btn_enable(b, on)
        # 恢复用户输入：必须先清空再写。建控件时有些框自带默认值（版本描述就是
        # "v0.1.0 first upload"），直接 insert 会变成两份，切一次语言多一份。
        for name, val in snap.items():
            w = getattr(self, name, None)
            if isinstance(w, (tk.Entry, ttk.Entry)):
                w.delete(0, tk.END)
                w.insert(0, val)
            elif isinstance(w, tk.Text):
                w.delete("1.0", tk.END)
                w.insert("1.0", val)
        self.login_status.set(tr(l_status))
        if l_guard:
            self.login_guard.delete(0, tk.END)
            self.login_guard.insert(0, l_guard)
        if l_guard_on:
            self.login_guard_frame.pack(fill=tk.X, pady=5, after=self.login_pass.master)
        if not l_btn_on:
            ui_btn_enable(self.login_btn, False)
            self.login_btn.configure(text=tr(l_btn_txt))
        if mode != "workshop":
            self._set_ul_mode(mode)
        self._check_ul_ready()
        if logged:
            self._login_done(silent=True)
        if log_text:
            self.console.configure(state=tk.NORMAL)
            self.console.insert(tk.END, log_text)
            self.console.configure(state=tk.DISABLED)
        if self._update_available:
            self._show_update_bar()
        self._refresh_nav()
        self.select_tab(tab if self._nav_enabled.get(tab) else 0)

    # ── 侧栏导航 ──
    def select_tab(self, idx):
        if not self._nav_enabled.get(idx, False):
            return
        for i, pg in enumerate(self._pages):
            if i == idx:
                pg.pack(fill=tk.BOTH, expand=True)
            else:
                pg.pack_forget()
        self._active_tab = idx
        self._refresh_nav()

    def set_tab_enabled(self, idx, on):
        self._nav_enabled[idx] = bool(on)
        if not on and self._active_tab == idx:
            self._active_tab = 0
            self.select_tab(0)
        self._refresh_nav()

    def _nav_hover(self, idx, on):
        if idx == self._active_tab or not self._nav_enabled.get(idx, False):
            return
        row, bar, lb = self._nav[idx]
        bgc = "#1c2530" if on else "#141821"
        row.configure(bg=bgc)
        bar.configure(bg=bgc)
        lb.configure(bg=bgc, fg="#c7d5e0" if on else "#7d8a96")

    def _refresh_nav(self):
        for idx, (row, bar, lb) in self._nav.items():
            enabled = self._nav_enabled.get(idx, False)
            if idx == self._active_tab:
                row.configure(bg=UI_BG_LIGHT)
                bar.configure(bg=UI_ACCENT)
                lb.configure(bg=UI_BG_LIGHT, fg="#ffffff",
                             font=("Microsoft YaHei", 10, "bold"))
            else:
                row.configure(bg="#141821")
                bar.configure(bg="#141821")
                lb.configure(bg="#141821", fg="#7d8a96" if enabled else "#3f4b56",
                             font=("Microsoft YaHei", 10))

    def _on_close(self):
        if self.running and not messagebox.askyesno(tr("确认"), tr("正在上传中，确定要关闭吗？"), parent=self.root):
            return
        if self.steamcmd:
            self.console_append(tr("正在关闭 SteamCMD..."))
            self.steamcmd.stop()
        self.root.destroy()

    def _startup(self):
        self.console_append("=" * 40); self.console_append(tr("定位 SteamCMD..."))
        path = SteamCMD.find_path(self.cfg.data.get("custom_steam_path"))
        if not path:
            self.console_append(tr("未找到 steamcmd.exe"))
            self.status.configure(text=tr("未找到 SteamCMD"))
            ui_btn_enable(self.home_btns[0], False)
            ui_btn_enable(self.home_btns[1], False)
            self._refresh_sc_path()
            self.root.after(80, self._show_missing_dialog)
            return
        self.cfg.data["custom_steam_path"] = path
        try:
            self.cfg.save()
        except Exception as e:
            log.warning("保存 steamcmd 路径失败: %s", e)
        self._refresh_sc_path()
        self.console_append(path)
        self.console_append(tr("正在启动 SteamCMD 自更新..."))
        self.status.configure(text=tr("SteamCMD 更新中..."))
        sc = SteamCMD(path)
        sc.on_output = lambda line: self.root.after(0, self.console_append, line)
        sc.start(extra_args=["+quit"])
        threading.Thread(target=self._wait_startup, args=(sc,), daemon=True).start()

    def _refresh_sc_path(self):
        lb = getattr(self, "sc_path_lb", None)
        if lb is None:
            return
        p = SteamCMD.find_path(self.cfg.data.get("custom_steam_path"))
        lb.configure(text=p or tr("未找到 steamcmd.exe"))

    def _show_missing_dialog(self):
        here = APP_DIR
        checked = [str(here / "steamcmd.exe"), str(here.parent / "steamcmd.exe")]
        dlg = MissingDialog(self, checked)
        dlg.show()
        self.root.wait_window(dlg)

    def _wait_startup(self, sc):
        if sc._reader_stop.wait(timeout=60):
            self.root.after(0, lambda: self.console_append(tr("初始化完成 — 请登录 Steam 账号")))
        else:
            self.root.after(0, lambda: self.console_append(tr("SteamCMD 自更新超时，仍可尝试登录")))
            sc.stop()
        self.root.after(0, lambda: ui_btn_enable(self.home_btns[0], True))
        self.root.after(0, lambda: self.set_tab_enabled(1, True))
        self.root.after(0, lambda: self.status.configure(text=tr("SteamCMD 就绪")))

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
        if t is None: t=[(tr("所有文件"),"*.*")]
        f = filedialog.askopenfilename(title=tr("选择文件"), filetypes=t)
        if f: e.delete(0, tk.END); e.insert(0, f)

    def _browse_dir(self, e):
        d = filedialog.askdirectory(title=tr("选择目录"))
        if d: e.delete(0, tk.END); e.insert(0, d)

    # ── 主页 ──────────────────────────────
    def _mk_card(self, parent, title):
        """创建 Steam 风格卡片，返回带 .body 的 Frame。"""
        card = tk.Frame(parent, bg=UI_BG_MID, highlightthickness=1, highlightbackground=UI_BORDER)
        tk.Label(card, text=title, bg=UI_BG_MID, fg="#ffffff", font=F_BOLD,
                 anchor="w").pack(fill=tk.X, padx=14, pady=(12, 8))
        body = tk.Frame(card, bg=UI_BG_MID)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 12))
        card.body = body
        return card

    def _build_home(self):
        page = self.tab_home
        _sf = ScrollFrame(page, bg=UI_BG_MID)
        _sf.pack(fill=tk.BOTH, expand=True)
        f = _sf.body

        title_row = tk.Frame(f, bg=UI_BG_MID)
        title_row.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Label(title_row, text=tr("主页"), bg=UI_BG_MID, fg="#ffffff",
                 font=("Microsoft YaHei", 12, "bold")).pack(side=tk.LEFT)
        tk.Frame(f, bg=UI_BORDER, height=1).pack(fill=tk.X, padx=16)

        # 安装进度卡片容器（按需显示）
        self.install_host = tk.Frame(f, bg=UI_BG_MID)
        self.install_host.pack(fill=tk.X, padx=14, pady=(12, 0))

        grid = tk.Frame(f, bg=UI_BG_MID)
        grid.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
        grid.columnconfigure(0, weight=3, uniform="c")
        grid.columnconfigure(1, weight=2, uniform="c")

        # ── 账号状态 ──
        c1 = self._mk_card(grid, tr("账号状态"))
        c1.grid(row=0, column=0, sticky="new", padx=(0, 8))

        row = tk.Frame(c1.body, bg=UI_BG_MID)
        row.pack(fill=tk.X)
        cv = tk.Canvas(row, width=34, height=34, bg=UI_BG_MID, highlightthickness=0)
        cv.pack(side=tk.LEFT, padx=(0, 10), anchor="n")
        cv.create_oval(2, 2, 32, 32, fill="#3a2023", outline="")
        cv.create_text(17, 17, text="!", fill="#e07a7a", font=("Arial", 12, "bold"))
        tb = tk.Frame(row, bg=UI_BG_MID)
        tb.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.home_state_title = tk.Label(tb, text=tr("尚未登录 Steam 账号"), bg=UI_BG_MID,
                                         fg="#ffffff", font=F_TXT, anchor="w")
        self.home_state_title.pack(anchor="w")
        self.home_state_sub = tk.Label(tb, text=tr("登录后即可上传 Workshop / SteamPipe"),
                                       bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w")
        self.home_state_sub.pack(anchor="w", pady=(2, 0))

        btns = tk.Frame(c1.body, bg=UI_BG_MID)
        btns.pack(fill=tk.X, pady=(16, 0))
        self.home_btns = []
        b_login = ui_btn(btns, tr("去登录"), "primary", lambda: self.select_tab(1))
        b_login.pack(side=tk.LEFT, padx=(0, 8))
        b_upload = ui_btn(btns, tr("去上传"), "secondary", lambda: self.select_tab(2))
        b_upload.pack(side=tk.LEFT, padx=(0, 8))
        ui_btn(btns, tr("设置"), "ghost", lambda: self.select_tab(3)).pack(side=tk.LEFT)
        self.home_btns += [b_login, b_upload]
        ui_btn_enable(b_login, False)
        ui_btn_enable(b_upload, False)

        # ── 快速开始 ──
        c2 = self._mk_card(grid, tr("快速开始"))
        c2.grid(row=0, column=1, sticky="new")
        # exe 版不用装 Python、也不用敲命令；py 版才需要
        if FROZEN:
            steps = [tr("将 {} 放到 steamcmd.exe 同目录").format(SELF_PATH.name),
                     tr("双击 {} 启动").format(SELF_PATH.name)]
        else:
            steps = [tr("将 {} 放到 steamcmd.exe 同目录").format(SELF_PATH.name),
                     tr("运行 pythonw {}").format(SELF_PATH.name)]
        steps.append(tr("登录 Steam → 选择上传方式 → 开始"))
        for i, txt in enumerate(steps):
            r = tk.Frame(c2.body, bg=UI_BG_MID)
            r.pack(fill=tk.X, pady=(0, 6))
            tk.Label(r, text=str(i + 1), bg=UI_BG_LIGHT, fg=UI_ACCENT, font=("Arial", 8, "bold"),
                     width=2).pack(side=tk.LEFT, padx=(0, 8), pady=(1, 0))
            tk.Label(r, text=txt, bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w",
                     justify=tk.LEFT, wraplength=210).pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ── 安装 / 复制流程 ────────────────────
    def _default_install_dir(self):
        p = self.cfg.data.get("custom_steam_path")
        if p:
            return os.path.dirname(p)
        return os.path.join(str(Path.home()), "SteamCMD")

    def ask_install_dir(self):
        dlg = PickDirDialog(self, self._default_install_dir())
        dlg.show()
        self.root.wait_window(dlg)
        if dlg.result:
            self.start_install(dlg.result)

    def ask_copy_to_existing(self):
        cur = SteamCMD.find_path(self.cfg.data.get("custom_steam_path"))
        dlg = CopyDialog(self, os.path.dirname(cur) if cur else "")
        dlg.show()
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        target, with_cfg = dlg.result
        try:
            written = SteamCmdInstaller.copy_self(target, with_cfg)
        except Exception as e:
            log.exception(tr("复制失败"))
            messagebox.showerror(tr("复制失败"), str(e), parent=self.root)
            return
        self.cfg.data["custom_steam_path"] = os.path.join(target, "steamcmd.exe")
        try:
            self.cfg.save()
        except Exception:
            pass
        self.console_append(tr("已复制本程序到 %s") % target)

        rows = []
        for w in written:
            rows.append((tr("文件"), w, tr("已复制")))
        rows.append(("SteamCMD", os.path.join(target, "steamcmd.exe"), tr("已找到")))
        dlg2 = DoneDialog(
            self, tr("本程序已复制到 SteamCMD 目录"), target, rows,
            action_text=tr("立即在新目录重启"),
            hint=tr("复制已完成，但当前运行的仍是旧位置的程序。点击「立即在新目录重启」"
                 "会在新目录重新启动（当前窗口关闭），账号与配置都会沿用。"))
        dlg2.show()
        self.root.wait_window(dlg2)
        if dlg2.result:
            try:
                SteamCmdInstaller.relaunch(target)
            except Exception as e:
                messagebox.showerror(tr("启动失败"), str(e), parent=self.root)
                return
            self.root.destroy()

    def download_zip_only(self):
        dest = filedialog.asksaveasfilename(
            title=tr("保存 steamcmd.zip"), defaultextension=".zip", initialfile="steamcmd.zip",
            filetypes=[(tr("ZIP 压缩包"), "*.zip"), (tr("所有文件"), "*.*")], parent=self.root)
        if not dest:
            return

        def work():
            try:
                SteamCmdInstaller.download(
                    STEAMCMD_ZIP_URL, dest,
                    on_progress=lambda d, t: self.root.after(
                        0, self.status.configure,
                        {"text": tr("下载中 %.1f / %.1f MB") % (d / 1048576, t / 1048576)}),
                    on_log=lambda s: self.root.after(0, self.console_append, s))
                self.root.after(0, lambda: messagebox.showinfo(
                    tr("下载完成"), tr("已保存到：\n") + dest, parent=self.root))
                self.root.after(0, lambda: self.status.configure(text=tr("就绪")))
            except Exception as e:
                log.exception(tr("下载 zip 失败"))
                self.root.after(0, lambda: messagebox.showerror(
                    tr("下载失败"), str(e), parent=self.root))
                self.root.after(0, lambda: self.status.configure(text=tr("下载失败")))

        threading.Thread(target=work, daemon=True, name="zip-dl").start()

    def start_install(self, target):
        if self._install_busy:
            return
        self._install_busy = True
        if self.install_panel is None:
            self.install_panel = InstallPanel(self.install_host, self)
        self.install_panel.pack(fill=tk.X)
        self.install_panel.start(target)
        self.select_tab(0)
        self.status.configure(text=tr("正在安装 SteamCMD…"))
        threading.Thread(target=self._install_worker, args=(target,),
                         daemon=True, name="installer").start()

    def _install_worker(self, target):
        p = self.install_panel
        cancel = p.cancel_evt

        def ui(fn, *a):
            self.root.after(0, fn, *a)

        def say(s, tag=None):
            ui(p.log, s, tag)

        try:
            os.makedirs(target, exist_ok=True)

            # ① 下载
            ui(p.set_step, 0, 1)
            say(tr("下载 ") + STEAMCMD_ZIP_URL, "dim")
            zip_path = os.path.join(target, "_steamcmd_dl.zip")
            last = [0.0]

            def prog(d, t):
                now = time.time()
                if now - last[0] > 0.12 or (t and d >= t):
                    last[0] = now
                    ui(p.set_progress, d, t)

            SteamCmdInstaller.download(STEAMCMD_ZIP_URL, zip_path, prog, say, cancel)
            ui(p.set_step, 0, 2)
            say(tr("✓ 下载完成"), "ok")

            # ② 解压
            ui(p.set_step, 1, 1)
            say(tr("解压到 ") + target, "dim")
            exe = SteamCmdInstaller.extract(zip_path, target)
            if not exe:
                raise RuntimeError(tr("压缩包内未找到 steamcmd.exe"))
            try:
                os.remove(zip_path)
            except OSError:
                pass
            ui(p.set_step, 1, 2)
            say(tr("✓ 解压完成，steamcmd.exe 已就位"), "ok")

            # ③ 首次初始化
            ui(p.set_step, 2, 1)
            ui(p.set_progress, 0, 0, tr("首次初始化：正在拉取运行环境…"))
            say(tr("运行 steamcmd.exe +quit（首次初始化）"), "dim")
            SteamCmdInstaller.initialize(exe, lambda s: say(s), cancel)
            ui(p.set_step, 2, 2)
            say(tr("✓ 初始化完成"), "ok")

            # ④ 复制本程序
            ui(p.set_step, 3, 1)
            written = SteamCmdInstaller.copy_self(target, True)
            for w in written:
                say("＋ " + w, "ok")
            if not written:
                say(tr("本程序已在目标目录，无需复制"), "dim")
            ui(p.set_step, 3, 2)
            ui(p.set_progress, 1, 1, tr("完成"))

            self.cfg.data["custom_steam_path"] = exe
            try:
                self.cfg.save()
            except Exception:
                pass
            ui(self._install_done, target)

        except InterruptedError:
            say(tr("已取消"), "warn")
            ui(p.finish_error, tr("用户已取消"))
            ui(self.status.configure, {"text": tr("安装已取消")})
        except Exception as e:
            log.exception(tr("安装失败"))
            say("✗ " + str(e), "warn")
            ui(p.finish_error, str(e))
            ui(self.status.configure, {"text": tr("安装失败")})
        finally:
            self._install_busy = False

    def _install_done(self, target):
        self.status.configure(text=tr("SteamCMD 就绪"))
        self.set_tab_enabled(1, True)
        ui_btn_enable(self.home_btns[0], True)
        ui_btn_enable(self.home_btns[1], True)
        if self.install_panel:
            self.install_panel.finish_success([], target)
        self.home_state_sub.configure(text=tr("安装完成，重启后即可登录上传"))

    def on_install_panel_closed(self):
        if self.install_panel:
            self.install_panel.pack_forget()

    # ── 上传 ──────────────────────────────
    def _ul_sub(self, parent, text):
        """小节标题 + 细分隔线。"""
        tk.Label(parent, text=text, bg=UI_BG_MID, fg=UI_ACCENT, anchor="w",
                 font=("Microsoft YaHei", 8, "bold")).pack(fill=tk.X, pady=(8, 2))
        tk.Frame(parent, bg=UI_BORDER, height=1).pack(fill=tk.X, pady=(0, 3))

    def _ul_field(self, parent, label, btn=None, hint=None):
        """一行「标签 + 输入框(+按钮/+提示)」，标签用定宽容器保证不截断、列对齐。"""
        r = tk.Frame(parent, bg=UI_BG_MID)
        r.pack(fill=tk.X, pady=2)
        lab = tk.Frame(r, bg=UI_BG_MID, width=(132 if LANG == "en" else 112), height=20)
        lab.pack(side=tk.LEFT)
        lab.pack_propagate(False)
        tk.Label(lab, text=label, bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S,
                 anchor="w").pack(fill=tk.BOTH, expand=True)
        e = tk.Entry(r, bg=UI_ENTRY, fg="#ffffff", font=F_TXT_S, relief=tk.FLAT,
                     insertbackground=UI_ACCENT, highlightthickness=1,
                     highlightbackground=UI_BORDER)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0), ipady=5)
        if btn:
            ui_btn(r, btn[0], "secondary", btn[1]).pack(side=tk.LEFT, padx=(8, 0))
        if hint:
            tk.Label(r, text=hint, bg=UI_BG_MID, fg="#5a6b7a",
                     font=F_TXT_S).pack(side=tk.LEFT, padx=(10, 0))
        return e

    def _build_upload(self):
        page = self.tab_upload

        # 底部固定操作条（始终可见，不随表单滚动）
        foot = tk.Frame(page, bg="#161c26", highlightthickness=1, highlightbackground=UI_BORDER)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        fin = tk.Frame(foot, bg="#161c26")
        fin.pack(fill=tk.X, padx=14, pady=10)

        self.ul_prog_host = tk.Frame(fin, bg="#161c26")
        self.ul_prog_host.pack(fill=tk.X)
        self.ul_progress_label = tk.Label(self.ul_prog_host, text="", bg="#161c26",
                                          fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w")
        self.ul_progress = ttk.Progressbar(self.ul_prog_host, mode="determinate", maximum=100)

        self.ul_btn = ui_btn(fin, tr("开始上传"), "green", self._start_upload, width=10)
        self.ul_btn.pack(side=tk.LEFT)
        tk.Label(fin, text=tr("将写入 VDF 配置并开始传输，过程输出见下方控制台"),
                 bg="#161c26", fg=UI_TEXT_DIM, font=F_TXT_S).pack(side=tk.LEFT, padx=(12, 0))

        _sf = ScrollFrame(page, bg=UI_BG_MID)
        _sf.pack(fill=tk.BOTH, expand=True)
        f = _sf.body

        title_row = tk.Frame(f, bg=UI_BG_MID)
        title_row.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Label(title_row, text=tr("上传"), bg=UI_BG_MID, fg="#ffffff",
                 font=("Microsoft YaHei", 12, "bold")).pack(side=tk.LEFT)
        tk.Frame(f, bg=UI_BORDER, height=1).pack(fill=tk.X, padx=16)

        # 登录状态条
        self._ul_warn_bar = tk.Frame(f, bg=UI_WARN_BG)
        self._ul_warn_bar.pack(fill=tk.X, padx=14, pady=(10, 0))
        self._ul_warn_accent = tk.Frame(self._ul_warn_bar, bg=UI_WARN, width=3)
        self._ul_warn_accent.pack(side=tk.LEFT, fill=tk.Y)
        self.ul_warn = tk.Label(self._ul_warn_bar, text=tr("⚠  需要先登录 Steam 账号"),
                                bg=UI_WARN_BG, fg=UI_WARN, font=F_TXT_S, anchor="w")
        self.ul_warn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=6)

        panel = tk.Frame(f, bg=UI_BG_MID, highlightthickness=1, highlightbackground=UI_BORDER)
        panel.pack(fill=tk.X, padx=14, pady=(10, 2))
        body = tk.Frame(panel, bg=UI_BG_MID)
        body.pack(fill=tk.X, padx=14, pady=12)

        # 模式：分段按钮（替代 ttk.Radiobutton）
        self.ul_mode = tk.StringVar(value="workshop")
        seg = tk.Frame(body, bg=UI_BG_MID)
        seg.pack(fill=tk.X)
        self._ul_seg = {
            "workshop": ui_btn(seg, tr("Workshop 工坊"), "primary",
                               lambda: self._set_ul_mode("workshop")),
            "steampipe": ui_btn(seg, tr("SteamPipe 正式版"), "secondary",
                                lambda: self._set_ul_mode("steampipe")),
        }
        self._ul_seg["workshop"].pack(side=tk.LEFT)
        self._ul_seg["steampipe"].pack(side=tk.LEFT, padx=(8, 0))

        self._ul_sub(body, tr("基础（必填）"))

        # App ID（两种模式共用）
        self.ul_appid_frame = tk.Frame(body, bg=UI_BG_MID)
        self.ul_appid_frame.pack(fill=tk.X)
        self.ul_appid = self._ul_field(self.ul_appid_frame, tr("游戏 App ID *"),
                                       btn=(tr("常用游戏"), self._pick_ul_game))

        # 内容文件夹（两种模式共用）
        self.ul_content_frame = tk.Frame(body, bg=UI_BG_MID)
        self.ul_content_frame.pack(fill=tk.X)
        self.ul_content = self._ul_field(
            self.ul_content_frame, tr("内容文件夹 *"),
            btn=(tr("浏览"), lambda: (self._browse_dir(self.ul_content), self._check_ul_ready())))

        # 模式专属区（同一容器内切换，保证位置固定）
        self.ul_mode_host = tk.Frame(body, bg=UI_BG_MID)
        self.ul_mode_host.pack(fill=tk.X)

        # Workshop 专用
        self.ul_ws_frame = tk.Frame(self.ul_mode_host, bg=UI_BG_MID)
        self._ul_sub(self.ul_ws_frame, tr("工坊信息"))
        self.ul_preview = self._ul_field(
            self.ul_ws_frame, tr("预览图片 *"),
            btn=(tr("浏览"), lambda: self._browse_file(self.ul_preview, [(tr("图片"), "*.jpg;*.png")])))
        self.ul_tags = self._ul_field(self.ul_ws_frame, tr("标签(分号分隔)"),
                                      hint=tr("如 Adventure;Co-op"))
        tk.Label(self.ul_ws_frame, text=tr("更新说明"), bg=UI_BG_MID, fg=UI_TEXT_DIM,
                 font=F_TXT_S, anchor="w").pack(fill=tk.X, pady=(8, 3))
        self.ul_note = tk.Text(self.ul_ws_frame, height=2, bg=UI_ENTRY, fg="#ffffff",
                               font=F_TXT_S, relief=tk.FLAT, bd=0, padx=8, pady=5,
                               wrap=tk.WORD, insertbackground=UI_ACCENT,
                               highlightthickness=1, highlightbackground=UI_BORDER)
        self.ul_note.pack(fill=tk.X)

        # SteamPipe 专用
        self.ul_sp_frame = tk.Frame(self.ul_mode_host, bg=UI_BG_MID)
        self._ul_sub(self.ul_sp_frame, tr("构建配置"))
        sp_depot = tk.Frame(self.ul_sp_frame, bg=UI_BG_MID)
        sp_depot.pack(fill=tk.X, pady=2)
        lab = tk.Frame(sp_depot, bg=UI_BG_MID, width=(132 if LANG == "en" else 112), height=20)
        lab.pack(side=tk.LEFT)
        lab.pack_propagate(False)
        tk.Label(lab, text="Depot ID *", bg=UI_BG_MID, fg=UI_TEXT_DIM,
                 font=F_TXT_S, anchor="w").pack(fill=tk.BOTH, expand=True)
        self.ul_depot = ttk.Combobox(sp_depot, state="readonly", width=16)
        self.ul_depot.pack(side=tk.LEFT, padx=(2, 0))
        ui_btn(sp_depot, tr("查询 Depots"), "secondary",
               self._fetch_depots).pack(side=tk.LEFT, padx=(8, 0))
        self.ul_depot.bind("<<ComboboxSelected>>", lambda e: self._check_ul_ready())

        self.ul_desc = self._ul_field(self.ul_sp_frame, tr("版本描述 *"))
        self.ul_desc.insert(0, "v0.1.0 first upload")

        base = APP_DIR / "output"
        self.ul_build_out = self._ul_field(
            self.ul_sp_frame, tr("输出目录 *"),
            btn=(tr("浏览"), lambda: (self._browse_dir(self.ul_build_out), self._check_ul_ready())))
        self.ul_build_out.insert(0, str(base))

        # 默认 Workshop 布局
        self._toggle_ul_mode()
        self.ul_btn.configure(state=tk.DISABLED)

        for e in (self.ul_appid, self.ul_content, self.ul_desc, self.ul_build_out):
            e.bind("<KeyRelease>", lambda ev: self._check_ul_ready())

    def _set_ul_mode(self, mode):
        if self.ul_mode.get() == mode:
            return
        self.ul_mode.set(mode)
        for k, b in self._ul_seg.items():
            b.set_kind("primary" if k == mode else "secondary")
        self._toggle_ul_mode()

    def _ul_warn_state(self, ok, text):
        """上传页顶部状态条：已登录转绿，未登录保持黄色警示。"""
        col = UI_GREEN if ok else UI_WARN
        bg = UI_GREEN_BG if ok else UI_WARN_BG
        try:
            self._ul_warn_accent.configure(bg=col)
            self._ul_warn_bar.configure(bg=bg)
            self.ul_warn.configure(text=text, fg=col, bg=bg)
        except AttributeError:
            pass

    def _check_ul_ready(self):
        if self.ul_mode.get() == "workshop":
            ok = bool(self.ul_appid.get().strip() and self.ul_content.get().strip())
        else:
            ok = bool(self.ul_appid.get().strip() and self.ul_depot.get().strip()
                      and self.ul_content.get().strip() and self.ul_desc.get().strip()
                      and self.ul_build_out.get().strip())
        self.ul_btn.configure(state=tk.NORMAL if ok else tk.DISABLED)

    def _toggle_ul_mode(self):
        for w in (self.ul_ws_frame, self.ul_sp_frame):
            w.pack_forget()
        if self.ul_mode.get() == "workshop":
            self.ul_ws_frame.pack(fill=tk.X)
        else:
            self.ul_sp_frame.pack(fill=tk.X)
        self._check_ul_ready()

    def _pick_ul_game(self):
        self.cfg.reload()
        apps = self.cfg.visible_apps()
        if not apps: messagebox.showinfo(tr("提示"), tr("请到设置页添加")); return
        d = tk.Toplevel(self.root); d.title(tr("选择游戏"))
        self._center(d, 350, 300)
        d.transient(self.root); d.grab_set()
        lb = tk.Listbox(d, font=("Consolas",10)); lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for id_, name in apps: lb.insert(tk.END, "[{}] {}".format(id_, name))
        def sel():
            i = lb.curselection()
            if i and i[0] < len(apps): self.ul_appid.delete(0, tk.END); self.ul_appid.insert(0, apps[i[0]][0])
            d.destroy(); self._check_ul_ready()
        rb(d, tr("确定"), sel, kind="primary").pack(pady=5)

    def _start_upload(self):
        if self.running: return
        app_id = self.ul_appid.get().strip()
        content = self.ul_content.get().strip()
        if not app_id or not content: messagebox.showwarning(tr("提示"), tr("请填写 App ID 和内容文件夹")); return
        if not os.path.isdir(content): messagebox.showwarning(tr("提示"), tr("内容文件夹不存在")); return
        if not self.steamcmd or not self.steamcmd.is_alive():
            messagebox.showwarning(tr("提示"), tr("SteamCMD 未运行，请先登录")); return

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
            self.root.after(0, lambda p=pct: self.ul_progress_label.configure(text=tr("上传中 {:.0f}%").format(p)))
        elif 'Build ID' in clean:
            self.root.after(0, lambda: self.ul_progress.configure(value=100))
            self.root.after(0, lambda: self.ul_progress_label.configure(text=tr("完成")))
        elif 'Scanning content' in clean or 'Building file mapping' in clean or 'Building depot' in clean:
            self.root.after(0, lambda: self.ul_progress.configure(mode='indeterminate'))
            self.root.after(0, lambda: self.ul_progress.start(10))
            self.root.after(0, lambda: self.ul_progress_label.configure(text=tr("准备中...")))

    def _reset_progress(self):
        self.ul_progress.stop()
        self.ul_progress.pack_forget()
        self.ul_progress_label.pack_forget()
        self.ul_progress.configure(value=0, mode='determinate')

    def _fetch_depots(self):
        app_id = self.ul_appid.get().strip()
        if not app_id:
            messagebox.showwarning(tr("提示"), tr("请先填写 App ID"), parent=self.root); return
        if not self.steamcmd or not self.steamcmd.is_alive():
            messagebox.showwarning(tr("提示"), tr("SteamCMD 未登录"), parent=self.root); return
        self.console_append(tr("> 正在查询 App {} 的 Depot 列表...").format(app_id))
        with self.steamcmd._lock:
            self.steamcmd._partial = b""
        while not self.steamcmd._buf.empty():
            try: self.steamcmd._buf.get_nowait()
            except: break
        self.steamcmd.send(f"app_info_print {app_id}")
        lines, matched = self.steamcmd.read_until(["steam>"], timeout=30)
        if not matched:
            self.console_append(tr("⏱ 查询超时")); return
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
            self.console_append(tr("未找到 Depot")); return
        self.console_append(tr("找到 {} 个 Depot: {}").format(len(depots), ", ".join(sorted(depots))))
        sorted_depots = sorted(depots)
        self.ul_depot["values"] = sorted_depots
        self.ul_depot.set(sorted_depots[0])
        self._check_ul_ready()

    def _start_ws_upload(self, app_id, content):
        vdf = make_workshop_vdf(app_id, content, self.ul_preview.get().strip(),
                                self.ul_note.get("1.0", tk.END).strip(), self.ul_tags.get().strip())
        self.console_append(tr("VDF 已创建: ") + vdf)
        self.console_append("> workshop_build_item " + vdf)
        self.running = True
        self.ul_progress.pack(pady=(0,5))
        self.ul_progress_label.pack()
        self.ul_progress.configure(value=0, mode='indeterminate')
        self.ul_progress.start(10)
        self.ul_progress_label.configure(text=tr("准备中..."))
        sc = self.steamcmd
        sc.send("workshop_build_item \"" + vdf + "\"")
        threading.Thread(target=self._upload_wait, args=(sc,), daemon=True).start()

    def _start_sp_upload(self, app_id, content):
        depot = self.ul_depot.get().strip()
        desc = self.ul_desc.get().strip()
        build_out = self.ul_build_out.get().strip()
        if not depot or not desc or not build_out:
            messagebox.showwarning(tr("提示"), tr("请填写 Depot ID / 版本描述 / 输出目录")); return
        scripts_dir = APP_DIR / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        Path(build_out).mkdir(parents=True, exist_ok=True)

        app_vdf = scripts_dir / f"app_build_{app_id}.vdf"
        depot_vdf = scripts_dir / f"depot_build_{depot}.vdf"
        content_root = os.path.abspath(content)

        depot_vdf.write_text(make_depot_build_vdf(depot), encoding="utf-8")
        app_vdf.write_text(make_app_build_vdf(app_id, desc, content_root,
                                              os.path.abspath(build_out), [depot]), encoding="utf-8")
        self.console_append(tr("脚本已生成: ") + app_vdf)
        self.console_append(tr("脚本已生成: ") + depot_vdf)
        self.console_append("> run_app_build " + str(app_vdf))
        self.running = True
        self.ul_progress.pack(pady=(0,5))
        self.ul_progress_label.pack()
        self.ul_progress.configure(value=0, mode='indeterminate')
        self.ul_progress.start(10)
        self.ul_progress_label.configure(text=tr("准备中..."))
        sc = self.steamcmd
        sc.send(f"run_app_build \"{app_vdf}\"")
        threading.Thread(target=self._upload_wait, args=(sc,), daemon=True).start()

    def _upload_wait(self, sc):
        try:
            lines, matched = sc.read_until(["FAILED", "ERROR", "Success", "complete", "BuildID"], timeout=600)
            log.info("[UPLOAD] 结果: matched=%s, lines=%d", matched, len(lines))
            if matched and ("FAILED" in matched or "ERROR" in matched):
                self.root.after(0, lambda m=matched: self.console_append(tr("❌ 上传失败: ") + m))
                self.root.after(0, lambda: messagebox.showerror(tr("上传失败"), matched, parent=self.root))
            elif matched and "BuildID" in matched:
                self.root.after(0, lambda: self.console_append(tr("✅ 上传完成 — 请到 Steamworks 后台设置 Live")))
                self.root.after(0, lambda: messagebox.showinfo(tr("上传成功"), tr("请在 Steamworks 后台设置 Live"), parent=self.root))
            elif matched:
                self.root.after(0, lambda: self.console_append(tr("✅ 上传完成")))
                self.root.after(0, lambda: messagebox.showinfo(tr("上传成功"), "", parent=self.root))
            else:
                self.root.after(0, lambda: self.console_append(tr("⏱ 超时，上传可能已完成")))
                self.root.after(0, lambda: messagebox.showwarning(tr("上传超时"), tr("上传可能已完成，请检查 Steamworks"), parent=self.root))
        except Exception as e:
            log.error("[UPLOAD] 异常: %s", e)
            self.root.after(0, lambda: self.console_append(tr("错误: ") + str(e)))
        finally:
            self.running = False
            self.root.after(0, self._reset_progress)

    # ── 登录 ──────────────────────────────
    def _build_login(self):
        f = self.tab_login
        self.login_status = tk.StringVar(value=tr("输入账号密码后点击登录"))
        tk.Label(f, textvariable=self.login_status, font=("微软雅黑",10), fg="#888").pack(pady=(10,0))

        card = ttk.LabelFrame(f, text=tr("登录")); card.pack(fill=tk.X, padx=10, pady=5)
        self.login_card = card

        # ── 表单内容 ──
        self.login_form_frame = ttk.Frame(card)
        r1 = ttk.Frame(self.login_form_frame); r1.pack(fill=tk.X, padx=15, pady=5)
        tk.Label(r1, text=tr("Steam 用户名:"), width=(16 if LANG == "en" else 14), anchor=tk.E).pack(side=tk.LEFT)
        self.login_user = ttk.Entry(r1, font=("微软雅黑",11))
        self.login_user.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.login_user.bind("<Return>", lambda e: self.login_pass.focus())
        self.login_user.focus()

        r2 = ttk.Frame(self.login_form_frame); r2.pack(fill=tk.X, padx=15, pady=5)
        tk.Label(r2, text=tr("密码:"), width=(16 if LANG == "en" else 14), anchor=tk.E).pack(side=tk.LEFT)
        self.login_pass = ttk.Entry(r2, show="*", font=("微软雅黑",11))
        self.login_pass.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.login_pass.bind("<Return>", lambda e: self._login_start())

        self.login_guard_frame = ttk.Frame(self.login_form_frame)
        tk.Frame(self.login_guard_frame, height=1, bg="#2a3f5a").pack(fill=tk.X, padx=15, pady=(8, 0))
        rg = ttk.Frame(self.login_guard_frame); rg.pack(fill=tk.X, padx=15, pady=(9, 2))
        tk.Label(rg, text=tr("Steam Guard 验证码"), font=("微软雅黑", 9, "bold"),
                 fg="#e8a735").pack(side=tk.LEFT)
        self.login_guard = ttk.Entry(rg, font=("Consolas", 15), width=10)
        self.login_guard.pack(side=tk.LEFT, padx=(12, 6))
        self.login_guard.bind("<Return>", lambda e: self._login_submit_guard())
        self.login_guard_btn = rb(rg, tr("确认"), self._login_submit_guard, kind="primary", minw=72)
        self.login_guard_btn.pack(side=tk.LEFT)
        tk.Label(self.login_guard_frame,
                 text=tr("5 位字母或数字。手机装过 Steam App 的，在 App 菜单 → Steam Guard 里查看；\n没绑手机令牌的，验证码会发到注册邮箱（记得看垃圾邮件箱）。"),
                 font=("微软雅黑", 8), fg="#8f98a0", justify=tk.LEFT,
                 anchor="w").pack(fill=tk.X, padx=15, pady=(2, 8))

        ba = ttk.Frame(self.login_form_frame); ba.pack(fill=tk.X, padx=15, pady=8)
        self.login_btn = rb(ba, tr("开始登录"), self._login_start, kind="primary", minw=120)
        self.login_btn.pack(side=tk.LEFT, padx=5)
        rb(ba, tr("保存账号"), self._save_login).pack(side=tk.LEFT, padx=5)
        self.login_form_frame.pack(fill=tk.X)

        # ── 已登录视图（同位置）──
        self.login_loggedin_frame = ttk.Frame(card)
        info = ttk.Frame(self.login_loggedin_frame); info.pack(fill=tk.X, padx=15, pady=10)
        tk.Label(info, text=tr("用户名:"), width=(12 if LANG == "en" else 10), anchor=tk.E).grid(row=0, column=0, sticky=tk.E, pady=3)
        self.login_loggedin_user = tk.Label(info, font=("微软雅黑",11), fg="#c7d5e0", anchor=tk.W)
        self.login_loggedin_user.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)
        tk.Label(info, text=tr("状态:"), width=(12 if LANG == "en" else 10), anchor=tk.E).grid(row=1, column=0, sticky=tk.E, pady=3)
        tk.Label(info, text=tr("✅ 已登录"), font=("微软雅黑",10), fg="#00cc66", anchor=tk.W).grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)
        ba2 = ttk.Frame(self.login_loggedin_frame); ba2.pack(fill=tk.X, padx=15, pady=8)
        rb(ba2, tr("退出登录"), self._logout).pack(side=tk.LEFT, padx=5)
        rb(ba2, tr("保存用户"), self._save_login).pack(side=tk.LEFT, padx=5)

        sf = ttk.LabelFrame(f, text=tr("已保存账号（双击自动登录）"))
        sf.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.login_list = tk.Listbox(sf, height=3, font=("Consolas",10))
        self.login_list.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.login_list.bind("<Double-Button-1>", self._login_from_saved)
        self._refresh_logins()
        bf = ttk.Frame(f); bf.pack(pady=5)
        rb(bf, tr("删除选中"), self._delete_login).pack(side=tk.LEFT, padx=5)
        rb(bf, tr("清除缓存"), self._clear_creds).pack(side=tk.LEFT, padx=5)

    def _start_steamcmd_with_args(self, extra_args):
        path = SteamCMD.find_path()
        if not path: return None
        if self.steamcmd: self.steamcmd.stop(); self.steamcmd = None
        sc = SteamCMD(path)
        sc.on_output = self._on_steamcmd_output
        sc.start(extra_args=extra_args)
        self.steamcmd = sc; return sc

    # 登录判定用的关键词
    _LOGIN_OK = ("waiting for user info", "logged in ok", "login success")
    _LOGIN_GUARD = ("steam guard", "two-factor", "guard code")
    _LOGIN_FAIL = ("invalid password", "login failure", "login failed",
                   "rate limit", "account logon denied", "invalid login auth code")

    def _login_start(self):
        u = self.login_user.get().strip(); pw = self.login_pass.get()
        if not u or not pw: return
        remember_secret(pw)          # 拿到密码的第一时间就登记脱敏，后面怎么记都漏不出去
        self.login_user_val = u
        self.console_append(tr("> (正在登录...)"))
        self.login_status.set(tr("正在登录，请耐心等待..."))
        self.login_btn.configure(state=tk.DISABLED, text=tr("登录中..."))
        # 密码绝不进命令行参数：参数会出现在进程列表和日志里。
        # 改为启动干净的 steamcmd，再在终端里发 `login 用户名`，密码走 send_secret。
        self._start_steamcmd_with_args([])
        threading.Thread(target=self._login_worker, args=(u, pw), daemon=True).start()

    def _login_worker(self, user, pw):
        sc = self.steamcmd
        if not sc:
            self.root.after(0, lambda: self._login_reset(tr("SteamCMD 未启动")))
            return
        try:
            sc.read_until(["steam>"], timeout=90)          # 等提示符（首启可能先自更新）
            sc.send("login " + user)                        # 命令行里只有用户名
            _, m = sc.read_until(["password:", *self._LOGIN_OK, *self._LOGIN_FAIL], timeout=90)
            low = (m or "").lower()
            if "password:" in low:
                sc.send_secret(pw)                          # 密码只存在于内存与终端输入
                _, m = sc.read_until([*self._LOGIN_GUARD, *self._LOGIN_OK, *self._LOGIN_FAIL],
                                     timeout=120)
                low = (m or "").lower()
            log.info("[LOGIN] 结果关键词: %s", m)
            if any(t in low for t in self._LOGIN_GUARD):
                self.root.after(0, self._login_show_guard)
            elif any(t in low for t in self._LOGIN_OK):
                self.root.after(0, self._login_done)
            elif "invalid password" in low:
                self.root.after(0, lambda: self._login_reset(tr("密码错误")))
            elif any(t in low for t in self._LOGIN_FAIL):
                self.root.after(0, lambda: self._login_reset(tr("登录失败")))
            else:
                self.root.after(0, lambda: self._login_reset(tr("超时或无响应")))
        except Exception as e:
            log.error("[LOGIN] 异常: %s", e)
            self.root.after(0, lambda: self.console_append(tr("错误: ") + str(e)))

    def _login_show_guard(self):
        self.login_status.set(tr("需要 Steam Guard 验证码，请输入后点击确认"))
        self.login_guard_frame.pack(fill=tk.X, pady=5, after=self.login_pass.master)
        self.login_guard.focus()

    def _login_submit_guard(self):
        code = self.login_guard.get().strip()
        if not code: return
        sc = self.steamcmd
        if not sc or not sc.is_alive():
            self._login_reset(tr("SteamCMD 已退出，请重新登录"))
            return
        self.console_append(tr("> (验证码已发送)"))
        self.login_status.set(tr("正在验证..."))
        self.login_guard_btn.configure(state=tk.DISABLED)
        self.login_guard.delete(0, tk.END)
        sc.send_secret(code)                                # 验证码同样不进日志
        threading.Thread(target=self._login_wait_done, daemon=True).start()

    def _login_wait_done(self):
        try:
            sc = self.steamcmd
            _, m = sc.read_until([*self._LOGIN_OK, *self._LOGIN_GUARD, *self._LOGIN_FAIL],
                                 timeout=120)
            low = (m or "").lower()
            log.info("[LOGIN] 验证码后结果关键词: %s", m)
            if any(t in low for t in self._LOGIN_OK):
                self.root.after(0, self._login_done)
            elif any(t in low for t in self._LOGIN_GUARD) or "login auth code" in low:
                self.root.after(0, lambda: self._login_reset(tr("验证码错误，请重试")))
            elif "invalid password" in low:
                self.root.after(0, lambda: self._login_reset(tr("密码错误")))
            else:
                self.root.after(0, lambda: self._login_reset(tr("登录失败")))
        except Exception as e:
            log.error("[LOGIN] 验证码后异常: %s", e)
            self.root.after(0, lambda: self.console_append(tr("错误: ") + str(e)))

    def _login_done(self, silent=False):
        self.login_guard_frame.pack_forget()
        if not silent:
            self.console_append("=" * 40); self.console_append(tr("✅ Steam 登录成功！")); self.console_append("=" * 40)
        self.login_form_frame.pack_forget()
        self.login_loggedin_user.configure(text=self.login_user_val)
        self.login_loggedin_frame.pack(fill=tk.X)
        self.login_status.set("")
        ui_btn_enable(self.home_btns[1], True)
        self.set_tab_enabled(2, True)
        self.login_indicator.configure(text="● " + self.login_user_val, fg="#00cc66")
        self._ul_warn_state(True, tr("✅  已登录: ") + self.login_user_val)
        self._refresh_logins()
        if not silent:
            messagebox.showinfo(tr("成功"), tr("Steam 登录成功！"), parent=self.root)

    def _logout(self):
        self.login_loggedin_frame.pack_forget()
        self.login_user.delete(0, tk.END)
        self.login_pass.delete(0, tk.END)
        self.login_form_frame.pack(fill=tk.X)
        self._login_reset(tr("已退出登录"))

    def _login_reset(self, msg=None):
        self.login_guard_frame.pack_forget()
        self.login_status.set(msg if msg else tr("输入账号密码后点击登录"))
        self.login_btn.configure(state=tk.NORMAL, text=tr("开始登录"))
        self.login_guard_btn.configure(state=tk.NORMAL)
        self.login_guard.delete(0, tk.END)
        self.login_user.configure(state=tk.NORMAL)
        self.login_pass.configure(state=tk.NORMAL)
        ui_btn_enable(self.home_btns[1], False)
        self.set_tab_enabled(2, False)
        self.login_indicator.configure(text=tr("未登录"), fg="#888")
        self._ul_warn_state(False, tr("⚠  需要先登录 Steam 账号"))

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
                display = (self._mask_user(u) + "  " if u else "") + tr("(非本机保存)")
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
                    messagebox.showwarning(tr("无法登录"), tr("非本机保存，请重新登录"), parent=self.root)
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
        if u: self.cfg.add_login(u, p); self._refresh_logins(); messagebox.showinfo(tr("成功"), tr("已保存"))

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
            self.root.after(0, lambda: messagebox.showinfo(tr("成功"), tr("缓存已清除")))
        threading.Thread(target=run, daemon=True).start()

    # ── 设置 ──────────────────────────────
    def _build_settings(self):
        page = self.tab_settings
        _sf = ScrollFrame(page, bg=UI_BG_MID)
        _sf.pack(fill=tk.BOTH, expand=True)
        f = _sf.body

        lf = ttk.LabelFrame(f, text=tr("界面语言"))
        lf.pack(fill=tk.X, padx=10, pady=(10, 5))
        lrow = tk.Frame(lf, bg=UI_BG_MID)
        lrow.pack(fill=tk.X, padx=8, pady=(8, 4))
        for code, name in LANGS:
            b = rb(lrow, name, lambda c=code: self._switch_lang(c),
                   kind="secondary" if code == LANG else "ghost")
            b.pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(lf, text=tr("切换后界面立即重建，不会影响登录状态与已填写的内容。"),
                 bg=UI_BG_MID, fg=UI_TEXT_DIM, font=F_TXT_S, anchor="w").pack(fill=tk.X, padx=8, pady=(0, 8))

        af = ttk.LabelFrame(f, text=tr("常用 App 列表管理")); af.pack(fill=tk.X, padx=10, pady=10)
        cols = ("id","name","show","note")
        bf2 = ttk.Frame(af); bf2.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        rb(bf2, tr("添加"), self._add_app).pack(side=tk.LEFT, padx=2)
        rb(bf2, tr("删除"), self._remove_app).pack(side=tk.LEFT, padx=2)
        tv_box = tk.Frame(af, bg=UI_BG_MID)
        tv_box.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(5, 0))
        self.app_tree = ttk.Treeview(tv_box, columns=cols, show="headings", height=4)
        self.app_tree.heading("id", text="App ID"); self.app_tree.heading("name", text=tr("名称")); self.app_tree.heading("show", text=tr("显示")); self.app_tree.heading("note", text=tr("备注"))
        self.app_tree.column("id", width=80); self.app_tree.column("name", width=160); self.app_tree.column("show", width=50); self.app_tree.column("note", width=150)
        self.app_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sc = SteamScrollbar(tv_box, command=self.app_tree.yview, bg=UI_BG_MID)
        self.app_tree.configure(yscrollcommand=sc.set); sc.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))
        self.app_tree.bind("<ButtonRelease-1>", self._on_app_click)
        self.app_tree.bind("<Double-Button-1>", self._edit_app)
        self._refresh_apps()

        sf = ttk.LabelFrame(f, text="SteamCMD"); sf.pack(fill=tk.X, padx=10, pady=5)
        self.sc_path_lb = tk.Label(sf, text=tr("检测中…"), font=("Consolas", 9), fg="#8f98a0", anchor="w")
        self.sc_path_lb.pack(fill=tk.X, padx=8, pady=(6, 2))
        srow = tk.Frame(sf)
        srow.pack(fill=tk.X, padx=8, pady=(2, 8))
        rb(srow, tr("重新检测"), self._startup).pack(side=tk.LEFT, padx=(0, 4))
        rb(srow, tr("安装 / 更换…"), self.ask_install_dir).pack(side=tk.LEFT, padx=(0, 4))
        rb(srow, tr("复制本程序到…"), self.ask_copy_to_existing).pack(side=tk.LEFT)

        self._refresh_sc_path()

        uf = ttk.LabelFrame(f, text=tr("更新")); uf.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(uf, text=tr("当前版本: ") + __version__, font=("微软雅黑",9), fg="#888").pack(pady=(5,0))
        rb(uf, tr("检查更新"), self._check_update).pack(pady=5)

    def _refresh_apps(self):
        log.debug("[REFRESH] 开始刷新 app 列表")
        for i in self.app_tree.get_children(): self.app_tree.delete(i)
        apps = self.cfg.apps
        log.debug("[REFRESH] apps=%d 条", len(apps))
        for a in apps:
            self.app_tree.insert("", tk.END, values=(a["id"], a["name"], "✓" if a.get("show",True) else "✗", a.get("note", "")))
        log.debug("[REFRESH] 完成")

    def _add_app(self):
        d = tk.Toplevel(self.root); d.title(tr("添加"))
        self._center(d, 350, 250)
        d.transient(self.root); d.grab_set()
        tk.Label(d, text=tr("添加 App"), font=("微软雅黑",11)).pack(pady=10)
        f1 = ttk.Frame(d); f1.pack(pady=5)
        tk.Label(f1, text="App ID:").pack(side=tk.LEFT)
        eid = ttk.Entry(f1, width=15); eid.pack(side=tk.LEFT, padx=5)
        f2 = ttk.Frame(d); f2.pack(pady=5)
        tk.Label(f2, text=tr("名称:")).pack(side=tk.LEFT)
        ename = ttk.Entry(f2, width=25); ename.pack(side=tk.LEFT, padx=5)
        f3 = ttk.Frame(d); f3.pack(pady=5)
        tk.Label(f3, text=tr("备注:")).pack(side=tk.LEFT)
        enote = ttk.Entry(f3, width=25); enote.pack(side=tk.LEFT, padx=5)
        def do():
            aid = eid.get().strip()
            if aid:
                apps = self.cfg.apps
                if any(str(a["id"])==aid for a in apps): messagebox.showinfo(tr("提示"), tr("已存在")); d.destroy(); return
                apps.append({"id":aid,"name":ename.get().strip() or "App "+aid,"show":True,"note":enote.get().strip()})
                self.cfg.apps = apps; self._refresh_apps(); d.destroy()
        rb(d, tr("添加"), do, kind="primary").pack(pady=10)

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
        d = tk.Toplevel(self.root); d.title(tr("编辑 App"))
        self._center(d, 350, 250)
        d.transient(self.root); d.grab_set()
        tk.Label(d, text=tr("编辑 App"), font=("微软雅黑",11)).pack(pady=10)
        f1 = ttk.Frame(d); f1.pack(pady=5)
        tk.Label(f1, text="App ID:").pack(side=tk.LEFT)
        eid = ttk.Entry(f1, width=15); eid.insert(0, aid); eid.pack(side=tk.LEFT, padx=5)
        f2 = ttk.Frame(d); f2.pack(pady=5)
        tk.Label(f2, text=tr("名称:")).pack(side=tk.LEFT)
        ename = ttk.Entry(f2, width=25); ename.insert(0, name); ename.pack(side=tk.LEFT, padx=5)
        f3 = ttk.Frame(d); f3.pack(pady=5)
        tk.Label(f3, text=tr("备注:")).pack(side=tk.LEFT)
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
        btn = rb(d, tr("保存"), do, kind="primary")
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
        if messagebox.askyesno(tr("确认"), tr("删除 [{}]?").format(aid)):
            self.cfg.apps = [a for a in self.cfg.apps if str(a["id"]) != str(aid)]; self._refresh_apps()

    def _check_update_startup(self):
        threading.Thread(target=self._do_check_update, args=(True,), daemon=True).start()

    def _check_update(self):
        self.status.configure(text=tr("正在检查更新..."))
        threading.Thread(target=self._do_check_update, args=(False,), daemon=True).start()

    def _do_check_update(self, silent=False):
        try:
            import urllib.request
            resp = urllib.request.urlopen(VERSION_URL, timeout=15)
            content = resp.read().decode("utf-8")
            m = re.search(r'__version__\s*=\s*"([^"]+)"', content)
            if not m:
                if not silent:
                    self.root.after(0, lambda: messagebox.showinfo(tr("检查更新"), tr("无法解析远程版本号"), parent=self.root))
                self.root.after(0, lambda: self.status.configure(text=tr("就绪")))
                return
            remote_ver = m.group(1)
            if not ver_is_newer(remote_ver, __version__):
                # 远端不比本地新：可能一样，也可能是本地已经比发布版更新（自己改过的开发版）。
                # 老代码只判断「不相等」就当有新版本，结果会提示「更新」到一个更旧的版本。
                self._update_available = None
                if not silent:
                    if remote_ver == __version__:
                        msg = tr("已是最新版本 ({})").format(__version__)
                    else:
                        msg = tr("本地版本 ({}) 比远端还新，无需更新").format(__version__)
                    self.root.after(0, lambda t=msg: messagebox.showinfo(tr("检查更新"), t, parent=self.root))
                self.root.after(0, lambda: self.status.configure(text=tr("就绪")))
                return
            self._update_available = (remote_ver, content)
            self.root.after(0, self._show_update_bar)
            if not silent:
                self.root.after(0, self._show_update_dialog)
        except Exception as e:
            if not silent:
                self.root.after(0, lambda: messagebox.showerror(tr("检查更新"), tr("更新检查失败:\n{}").format(e), parent=self.root))
            self.root.after(0, lambda: self.status.configure(text=tr("就绪")))

    def _show_update_bar(self):
        self._update_bar.pack(fill=tk.X, before=self.page_host)

    def _show_update_dialog(self):
        if not self._update_available:
            return
        remote_ver, content = self._update_available
        if not messagebox.askyesno(tr("发现新版本"),
                tr("当前版本: {}\n最新版本: {}\n\n是否更新？").format(__version__, remote_ver),
                parent=self.root):
            return
        self._do_update(content)
    def _do_update(self, content):
        if FROZEN:
            # 自更新拉的是仓库里的 .py 源码，把它盖到 exe 上只会把 exe 变成一堆文本。
            # exe 版只能引导去下载新版，不能就地换文件。
            messagebox.showinfo(tr("提示"),
                tr("exe 版不支持自动更新。请到 GitHub 仓库下载最新版 exe 覆盖本文件。"),
                parent=self.root)
            return
        try:
            self_path = SELF_PATH
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
            messagebox.showerror(tr("更新失败"), str(e), parent=self.root)

    def run(self):
        self.root.mainloop()


# ── 英文词表（中文原文即 key）──────────────
_EN.update({
    'DPAPI 加密失败': 'DPAPI encryption failed',
    'DPAPI 解密失败': 'DPAPI decryption failed',
    '错误': 'Error',
    '缺少 pywinpty，自动安装失败，请手动运行:\n\npip install pywinpty': 'pywinpty is missing and the auto-install failed. Please run:\n\npip install pywinpty',
    'ConPTY 启动失败': 'ConPTY failed to start',
    '已取消下载': 'Download cancelled',
    '已取消初始化': 'Initialization cancelled',
    '初始化超时': 'Initialization timed out',
    '未检测到 SteamCMD': 'SteamCMD not found',
    '当前程序所在目录及其上级目录中，': "This program's folder and all parent folders",
    '均未找到 steamcmd.exe。': 'contain no steamcmd.exe.',
    '已检查路径': 'Checked paths',
    '自动复制过去…': 'Copy it for me…',
    '如果你已经下载并安装过 SteamCMD，只需把本程序（steamcmd_gui.py）放到 steamcmd.exe 所在目录，再重新打开即可。': 'If SteamCMD is already installed, just put this program (steamcmd_gui.py) in the same folder as steamcmd.exe and reopen it.',
    '若尚未安装：SteamCMD 是 Valve 官方提供的命令行工具，本程序需要它来完成 Steam 登录与内容上传。': "Not installed yet? SteamCMD is Valve's official command-line tool; this program needs it to sign in and upload content.",
    '是否现在下载并安装？': 'Download and install it now?',
    '复制': 'Copy',
    'SteamCMD 官方使用文档 ↗': 'SteamCMD official docs ↗',
    '若网络受限导致下载失败，可手动用浏览器打开上面的地址，解压后把 steamcmd.exe 放到本程序同目录。': 'If the network blocks the download, open the address above in a browser, unzip it and put steamcmd.exe next to this program.',
    '下载': 'Download',
    '稍后再说': 'Not now',
    '已复制': 'Copied',
    '下载并安装到指定目录…': 'Download and install to a folder…',
    '选择目录 → 自动下载 / 解压 / 初始化，并把本程序复制过去': 'Pick a folder → auto download / unzip / init, and copy this program there',
    '仅下载 zip 文件…': 'Download the zip only…',
    '保存到磁盘，稍后自己解压使用': 'Save it to disk and unzip it yourself later',
    '在浏览器打开官方下载页': 'Open the official download page in a browser',
    '选择 SteamCMD 安装目录': 'Choose SteamCMD install folder',
    'SteamCMD 将解压到该目录；安装成功后，本程序（steamcmd_gui.py）也会被复制到这里。': 'SteamCMD will be unzipped here; after a successful install this program (steamcmd_gui.py) is copied here too.',
    '浏览…': 'Browse…',
    '默认位置': 'Default location',
    '本地应用数据': 'Local app data',
    '安装包 774 KB': 'Package 774 KB',
    '解压后 约 3 MB': '~3 MB unzipped',
    '首次初始化 约 300 MB': '~300 MB first init',
    '目录已有同名文件时会被覆盖，其它文件不受影响。首次初始化需要联网，耗时通常 1–5 分钟。': 'Files with the same name will be overwritten; nothing else is touched. The first init needs network and usually takes 1–5 minutes.',
    '开始下载并安装': 'Download and install',
    '取消': 'Cancel',
    '选择安装目录': 'Choose install folder',
    '把本程序复制到 SteamCMD 目录': 'Copy this program to the SteamCMD folder',
    '选择 steamcmd.exe 所在的文件夹，本程序会被复制过去，之后从那里启动就能找到 SteamCMD。': 'Pick the folder that contains steamcmd.exe; this program will be copied there, so it can find SteamCMD when launched from there.',
    '同时带上现有配置 steamcmd_gui.cfg（保留账号与上传设置）': 'Also copy the current config steamcmd_gui.cfg (keeps accounts and upload settings)',
    '将写入': 'Will write',
    '复制过去': 'Copy',
    '选择 steamcmd.exe 所在目录': 'Choose the folder containing steamcmd.exe',
    '请选择 steamcmd.exe 所在目录': 'Please choose the folder containing steamcmd.exe',
    '✓  已找到 ': '✓  Found ',
    '✕  该目录下没有 steamcmd.exe，请重新选择': '✕  No steamcmd.exe in that folder, please choose again',
    '解压': 'Unzip',
    '首次初始化': 'First init',
    '复制本程序': 'Copy program',
    '正在安装 SteamCMD': 'Installing SteamCMD',
    '准备中…': 'Preparing…',
    '0 / 4 步': '0 / 4 steps',
    '安装过程中请勿关闭窗口': 'Do not close this window while installing',
    '立即在新目录重启': 'Restart in the new folder',
    '稍后': 'Later',
    '目标目录  ': 'Target  ',
    '%d / 4 步': '%d / 4 steps',
    'SteamCMD 安装完成': 'SteamCMD installed',
    '已就绪，可以登录 Steam 了': 'Ready — you can sign in to Steam now',
    '完成': 'Done',
    '4 / 4 步': '4 / 4 steps',
    '也可以稍后手动双击目标目录下的 ': 'You can also double-click ',
    ' 启动': ' in the target folder later',
    '安装未完成': 'Install not finished',
    '已中止': 'Aborted',
    '可以重试，或手动下载 zip 解压后把 steamcmd.exe 放到本程序目录': 'Retry, or download the zip manually and put steamcmd.exe next to this program',
    '正在取消…': 'Cancelling…',
    '启动失败': 'Launch failed',
    '无法在新目录启动：\n%s': 'Cannot launch in the new folder:\n%s',
    '关闭': 'Close',
    '就绪': 'Ready',
    '未登录': 'Not signed in',
    '主页': 'Home',
    '登录': 'Sign in',
    '上传': 'Upload',
    '设置': 'Settings',
    '↗ 新版本可用': '↗ Update available',
    '控制台输出': 'Console output',
    '确认': 'Confirm',
    '正在上传中，确定要关闭吗？': 'Upload in progress. Close anyway?',
    '正在关闭 SteamCMD...': 'Shutting down SteamCMD...',
    '定位 SteamCMD...': 'Locating SteamCMD...',
    '未找到 steamcmd.exe': 'steamcmd.exe not found',
    '未找到 SteamCMD': 'SteamCMD not found',
    '正在启动 SteamCMD 自更新...': 'Starting SteamCMD self-update...',
    'SteamCMD 更新中...': 'SteamCMD updating...',
    '初始化完成 — 请登录 Steam 账号': 'Initialized — please sign in to Steam',
    'SteamCMD 自更新超时，仍可尝试登录': 'SteamCMD self-update timed out, you can still try signing in',
    'SteamCMD 就绪': 'SteamCMD ready',
    '所有文件': 'All files',
    '选择文件': 'Choose file',
    '选择目录': 'Choose folder',
    '账号状态': 'Account',
    '尚未登录 Steam 账号': 'Not signed in to Steam',
    '登录后即可上传 Workshop / SteamPipe': 'Sign in to upload Workshop / SteamPipe',
    '去登录': 'Sign in',
    '去上传': 'Upload',
    '快速开始': 'Quick start',
    '将 {} 放到 steamcmd.exe 同目录': 'Put {} in the same folder as steamcmd.exe',
    '运行 pythonw {}': 'Run pythonw {}',
    '双击 {} 启动': 'Double-click {} to start',
    '登录 Steam → 选择上传方式 → 开始': 'Sign in → pick upload mode → start',
    '复制失败': 'Copy failed',
    '已复制本程序到 %s': 'Copied this program to %s',
    '文件': 'File',
    '已找到': 'Found',
    '本程序已复制到 SteamCMD 目录': 'Program copied to the SteamCMD folder',
    '复制已完成，但当前运行的仍是旧位置的程序。点击「立即在新目录重启」会在新目录重新启动（当前窗口关闭），账号与配置都会沿用。': 'Copy finished, but the running instance is still the old one. Click "Restart in the new folder" to relaunch there (this window closes); accounts and config carry over.',
    '保存 steamcmd.zip': 'Save steamcmd.zip',
    'ZIP 压缩包': 'ZIP archive',
    '下载中 %.1f / %.1f MB': 'Downloading %.1f / %.1f MB',
    '下载完成': 'Download complete',
    '已保存到：\n': 'Saved to:\n',
    '下载 zip 失败': 'Failed to download zip',
    '下载失败': 'Download failed',
    '正在安装 SteamCMD…': 'Installing SteamCMD…',
    '下载 ': 'Downloading ',
    '✓ 下载完成': '✓ Download complete',
    '解压到 ': 'Unzipping to ',
    '压缩包内未找到 steamcmd.exe': 'steamcmd.exe not found in the archive',
    '✓ 解压完成，steamcmd.exe 已就位': '✓ Unzipped, steamcmd.exe is in place',
    '首次初始化：正在拉取运行环境…': 'First init: fetching runtime…',
    '运行 steamcmd.exe +quit（首次初始化）': 'Running steamcmd.exe +quit (first init)',
    '✓ 初始化完成': '✓ Initialized',
    '本程序已在目标目录，无需复制': 'Program already in the target folder, nothing to copy',
    '已取消': 'Cancelled',
    '用户已取消': 'Cancelled by user',
    '安装已取消': 'Install cancelled',
    '安装失败': 'Install failed',
    '安装完成，重启后即可登录上传': 'Installed — restart to sign in and upload',
    '开始上传': 'Start upload',
    '将写入 VDF 配置并开始传输，过程输出见下方控制台': 'Writes the VDF configs and starts the transfer; progress shows in the console below',
    '⚠  需要先登录 Steam 账号': '⚠  Sign in to Steam first',
    'Workshop 工坊': 'Workshop',
    'SteamPipe 正式版': 'SteamPipe',
    '基础（必填）': 'Basics (required)',
    '游戏 App ID *': 'Game App ID *',
    '常用游戏': 'My games',
    '内容文件夹 *': 'Content folder *',
    '浏览': 'Browse',
    '工坊信息': 'Workshop info',
    '预览图片 *': 'Preview image *',
    '图片': 'Images',
    '标签(分号分隔)': 'Tags (; separated)',
    '如 Adventure;Co-op': 'e.g. Adventure;Co-op',
    '更新说明': 'Change note',
    '构建配置': 'Build config',
    '查询 Depots': 'Fetch depots',
    '版本描述 *': 'Build description *',
    '输出目录 *': 'Output folder *',
    '提示': 'Notice',
    '请到设置页添加': 'Add one in Settings first',
    '选择游戏': 'Choose a game',
    '确定': 'OK',
    '请填写 App ID 和内容文件夹': 'Please fill in App ID and content folder',
    '内容文件夹不存在': 'Content folder does not exist',
    'SteamCMD 未运行，请先登录': 'SteamCMD is not running, please sign in first',
    '准备中...': 'Preparing...',
    '请先填写 App ID': 'Please fill in App ID first',
    'SteamCMD 未登录': 'SteamCMD is not signed in',
    '⏱ 查询超时': '⏱ Query timed out',
    '未找到 Depot': 'No depot found',
    'VDF 已创建: ': 'VDF created: ',
    '请填写 Depot ID / 版本描述 / 输出目录': 'Please fill in Depot ID / build description / output folder',
    '❌ 上传失败: ': '❌ Upload failed: ',
    '上传失败': 'Upload failed',
    '✅ 上传完成 — 请到 Steamworks 后台设置 Live': '✅ Upload complete — set it Live in Steamworks',
    '上传成功': 'Upload succeeded',
    '请在 Steamworks 后台设置 Live': 'Set it Live in Steamworks',
    '✅ 上传完成': '✅ Upload complete',
    '⏱ 超时，上传可能已完成': '⏱ Timed out, the upload may have finished',
    '上传超时': 'Upload timed out',
    '上传可能已完成，请检查 Steamworks': 'The upload may have finished — check Steamworks',
    '错误: ': 'Error: ',
    '输入账号密码后点击登录': 'Enter your account and password, then click Sign in',
    'Steam 用户名:': 'Steam username:',
    '密码:': 'Password:',
    'Steam Guard 验证码': 'Steam Guard code',
    '5 位字母或数字。手机装过 Steam App 的，在 App 菜单 → Steam Guard 里查看；\n没绑手机令牌的，验证码会发到注册邮箱（记得看垃圾邮件箱）。': '5 letters or digits. With the Steam app on the phone, open the menu → Steam Guard.\nWithout a mobile authenticator the code goes to the registered email (check spam).',
    '开始登录': 'Sign in',
    '保存账号': 'Save account',
    '用户名:': 'Username:',
    '状态:': 'Status:',
    '✅ 已登录': '✅ Signed in',
    '退出登录': 'Sign out',
    '保存用户': 'Save user',
    '已保存账号（双击自动登录）': 'Saved accounts (double-click to sign in)',
    '删除选中': 'Delete selected',
    '清除缓存': 'Clear cache',
    '> (正在登录...)': '> (signing in...)',
    '正在登录，请耐心等待...': 'Signing in, please wait...',
    '登录中...': 'Signing in...',
    'SteamCMD 未启动': 'SteamCMD is not running',
    '密码错误': 'Wrong password',
    '登录失败': 'Sign-in failed',
    '超时或无响应': 'Timed out or no response',
    '需要 Steam Guard 验证码，请输入后点击确认': 'Steam Guard code required — enter it and click confirm',
    'SteamCMD 已退出，请重新登录': 'SteamCMD has exited, please sign in again',
    '> (验证码已发送)': '> (code sent)',
    '正在验证...': 'Verifying...',
    '验证码错误，请重试': 'Wrong code, please try again',
    '✅ Steam 登录成功！': '✅ Signed in to Steam!',
    '✅  已登录: ': '✅  Signed in: ',
    '成功': 'Success',
    'Steam 登录成功！': 'Signed in to Steam!',
    '已退出登录': 'Signed out',
    '(非本机保存)': '(not saved on this PC)',
    '无法登录': 'Cannot sign in',
    '非本机保存，请重新登录': 'Saved on another PC, please sign in again',
    '已保存': 'Saved',
    '缓存已清除': 'Cache cleared',
    '常用 App 列表管理': 'App list',
    '添加': 'Add',
    '删除': 'Delete',
    '名称': 'Name',
    '显示': 'Show',
    '备注': 'Note',
    '检测中…': 'Checking…',
    '重新检测': 'Re-detect',
    '安装 / 更换…': 'Install / change…',
    '复制本程序到…': 'Copy program to…',
    '更新': 'Updates',
    '检查更新': 'Check for updates',
    '添加 App': 'Add app',
    '名称:': 'Name:',
    '备注:': 'Note:',
    '已存在': 'Already exists',
    '编辑 App': 'Edit app',
    '保存': 'Save',
    '删除 [{}]?': 'Delete [{}]?',
    '正在检查更新...': 'Checking for updates...',
    '无法解析远程版本号': 'Cannot parse the remote version',
    '发现新版本': 'Update available',
    '更新失败': 'Update failed',
    'exe 版不支持自动更新。请到 GitHub 仓库下载最新版 exe 覆盖本文件。': 'The exe build cannot update itself. Download the latest exe from GitHub and replace this file.',
    '> 正在查询 App {} 的 Depot 列表...': '> Querying depots for app {}...',
    '找到 {} 个 Depot: {}': 'Found {} depots: {}',
    '脚本已生成: ': 'Script written: ',
    '当前版本: ': 'Version: ',
    '上传中 {:.0f}%': 'Uploading {:.0f}%',
    '已是最新版本 ({})': 'Already up to date ({})',
    '本地版本 ({}) 比远端还新，无需更新': 'Local version ({}) is newer than the remote one — nothing to update',
    '更新检查失败:\n{}': 'Update check failed:\n{}',
    '当前版本: {}\n最新版本: {}\n\n是否更新？': 'Current version: {}\nLatest version: {}\n\nUpdate now?',
    '切换后界面立即重建，不会影响登录状态与已填写的内容。': 'The UI rebuilds immediately; sign-in state and your input are kept.',
    '安装或上传正在进行，等它结束后再切换语言': 'Install or upload in progress — switch language after it finishes',
    '界面语言': 'Language',
})

_EN_REV.update({v: k for k, v in _EN.items()})

# ── 入口 ──────────────────────────────────
if __name__ == "__main__":
    App().run()
