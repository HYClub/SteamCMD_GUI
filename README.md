# SteamCMD GUI

Windows 下的 SteamCMD 图形界面工具，基于 ConPTY (pywinpty) 实现终端交互。

## 功能

- **Steam 登录** — 用户名/密码 + Steam Guard 验证码，凭据 DPAPI 加密保存，多账号管理
- **Workshop 工坊上传** — 预览图、标签、更新说明，走 `workshop_build_item`
- **SteamPipe 正式版上传** — 自动生成 `app_build.vdf` / `depot_build.vdf`，执行 `+run_app_build`
- **Depot 查询** — 通过 `app_info_print` 获取 App 的 Depot 列表，下拉选择
- **实时控制台** — SteamCMD 输出实时显示，上传进度条
- **常用 App 列表** — 管理 AppID/备注，快速填入
- **自更新** — 启动时后台检查 GitHub 版本，发现新版本顶部提示
- **安全退出** — 关闭时自动终止 SteamCMD 进程

## 使用

1. 将 `steamcmd_gui.py` 放到 `steamcmd.exe` 同目录
2. 双击 `SteamCMD_GUI.bat` 或运行 `pythonw steamcmd_gui.py`
3. 缺 `pywinpty` 时会自动安装

## 依赖

- Python 3.8+
- pywinpty（自动安装）
- Windows（DPAPI 加密 / ConPTY）
