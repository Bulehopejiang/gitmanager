@echo off
rem 启动 Git 管理器（Windows 双击脚本）
cd /d "%~dp0"

rem 检查 git 是否可用
where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 git 命令，请先安装 Git 并加入 PATH: https://git-scm.com
    pause
    exit /b 1
)

rem 优先用 python，其次用 py 启动器
where python >nul 2>nul
if not errorlevel 1 (
    python git_manager.py %*
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py git_manager.py %*
    ) else (
        echo [错误] 未找到 Python，请安装 Python 3.8+: https://www.python.org
        pause
        exit /b 1
    )
)

if errorlevel 1 pause
