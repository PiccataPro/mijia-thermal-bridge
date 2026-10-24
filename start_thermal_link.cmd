@echo off
rem 备用启动器：VBS 被策略拦截时使用。会有一次极短的控制台闪现。
rem 复用 thermal_link.py 的 PID 锁做单实例保障，重复启动会自行退出。
cd /d "%~dp0"
start "" /b ".venv\Scripts\pythonw.exe" "thermal_link.py" --loop
exit /b 0
