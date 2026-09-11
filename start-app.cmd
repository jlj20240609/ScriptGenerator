@echo off
rem 一键启动「脚本构建器」（双击本文件即可）。等价于在 app 目录执行 npm start。
rem 为什么要这个：引擎/界面都在本机跑，不需要我或任何工具介入；想开就自己开。
setlocal
cd /d "%~dp0app"
if not exist "node_modules\.bin\electron.cmd" (
  echo [x] 还没装依赖。先在这个目录执行：  npm install
  echo.
  pause
  exit /b 1
)
echo 正在启动「脚本构建器」……（关掉窗口即退出）
call "node_modules\.bin\electron.cmd" .
if errorlevel 1 (
  echo.
  echo [x] 启动失败，错误码 %errorlevel%。上面应有原因。
  pause
)
endlocal
