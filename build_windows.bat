@echo off
chcp 65001 >nul
REM ============================================================
REM 竞品订阅解锁工具 一键打包（Windows）
REM 产物: dist\竞品订阅解锁工具.exe —— 单文件，同事双击即用，
REM 内置 JRE/apktool/apksigner/adb + frida 客户端，无需装任何依赖。
REM
REM 前置：本机装过 Python 3.10+（勾选 Add to PATH）
REM ============================================================
cd /d %~dp0

echo [1/4] 创建打包环境 ...
python -m venv build_venv || goto :err
call build_venv\Scripts\activate.bat

echo [2/4] 安装打包依赖（pyinstaller + frida 客户端）...
python -m pip install -U pip -q
python -m pip install -q pyinstaller frida || goto :err

echo [3/4] PyInstaller 打包 ...
python -m PyInstaller --onefile --windowed --clean --noconfirm ^
  --distpath dist_onefile ^
  --name "竞品订阅解锁工具" ^
  --hidden-import frida --collect-all frida ^
  launcher.py || goto :err

echo [4/4] 合并内置 runtime（JRE/apktool/apksigner/adb）...
python make_exe.py "dist_onefile\竞品订阅解锁工具.exe" runtime "dist\竞品订阅解锁工具.exe" || goto :err

echo.
echo ✔ 完成: dist\竞品订阅解锁工具.exe
echo   把该文件发同事即可（首次运行自动解压内置组件，约 30 秒）。
goto :eof

:err
echo ✗ 打包失败，请检查上方报错
exit /b 1
