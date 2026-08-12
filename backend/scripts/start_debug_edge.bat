@echo off
REM ============================================================
REM 启动调试模式 Edge（CDP 登录态方案，与既有项目一致）
REM - 独立用户目录 backend\data\edge_debug_profile（登录态持久化于此）
REM - 调试端口 9222，Playwright 通过 CDP 接管此浏览器
REM - 启动后请访问点评商家后台，必要时手动登录（登录态会自动保存）
REM ============================================================
set "PROFILE=%~dp0..\data\edge_debug_profile"
if not exist "%PROFILE%" mkdir "%PROFILE%"
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
echo.
echo Edge 调试模式已启动（端口 9222，profile: %PROFILE%）
echo 之后运行：python -m scripts.import_login_state --port 9222 --cookies "你的cookies.json路径"
pause
