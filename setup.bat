@echo off
echo ====================================
echo Setup TileServer GL + OpenMapTiles
echo ====================================

REM Kiểm tra Node.js
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js không được cài đặt
    echo Vui lòng cài đặt Node.js từ https://nodejs.org/
    pause
    exit /b 1
)

REM Kiểm tra Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python không được cài đặt
    echo Vui lòng cài đặt Python từ https://python.org/
    pause
    exit /b 1
)

echo [1/5] Cài đặt TileServer GL Light...
npm install -g tileserver-gl-light
if %errorlevel% neq 0 (
    echo [ERROR] Không thể cài đặt TileServer GL Light
    pause
    exit /b 1
)

echo [2/5] Cài đặt Python dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Không thể cài đặt Python packages
    pause
    exit /b 1
)

echo [3/5] Kiểm tra cấu trúc thư mục...
if not exist "data\" mkdir data
if not exist "styles\" mkdir styles
if not exist "fonts\" mkdir fonts

echo [4/5] Kiểm tra TileServer GL config...
if not exist "tileserver-config.json" (
    echo [ERROR] Thiếu file tileserver-config.json
    echo Vui lòng tạo file config trước
    pause
    exit /b 1
)

if not exist "styles\vietnam.json" (
    echo [ERROR] Thiếu file styles\vietnam.json
    echo Vui lòng tạo file style trước
    pause
    exit /b 1
)

echo [5/5] Tạo script khởi động...

REM Tạo script để start TileServer GL
echo @echo off > start-tileserver.bat
echo echo Starting TileServer GL on port 8080... >> start-tileserver.bat
echo tileserver-gl-light --config tileserver-config.json --port 8080 >> start-tileserver.bat

REM Tạo script để start Flask app
echo @echo off > start-flask.bat
echo echo Starting Flask app on port 5000... >> start-flask.bat
echo set FLASK_ENV=development >> start-flask.bat
echo python app.py >> start-flask.bat

REM Tạo script để start cả hai
echo @echo off > start-all.bat
echo echo Starting TileServer GL and Flask app... >> start-all.bat
echo start "TileServer GL" cmd /c start-tileserver.bat >> start-all.bat
echo timeout /t 3 /nobreak >> start-all.bat
echo start "Flask App" cmd /c start-flask.bat >> start-all.bat
echo echo. >> start-all.bat
echo echo Both services are starting... >> start-all.bat
echo echo TileServer GL: http://localhost:8080 >> start-all.bat
echo echo Flask App: http://localhost:5000 >> start-all.bat
echo pause >> start-all.bat

echo.
echo ====================================
echo Setup hoàn thành!
echo ====================================
echo.
echo Để tải OpenMapTiles data cho Việt Nam:
echo 1. Truy cập https://openmaptiles.org/downloads/
echo 2. Tải file vietnam.mbtiles 
echo 3. Đặt vào thư mục data\vietnam.mbtiles
echo.
echo Hoặc tự build từ OSM data:
echo 1. git clone https://github.com/openmaptiles/openmaptiles.git
echo 2. Chỉnh BBOX=102.0,5.0,118.8,25.2 trong .env
echo 3. Chạy make để build tiles
echo.
echo Để khởi động:
echo - Chỉ TileServer GL: start-tileserver.bat
echo - Chỉ Flask app: start-flask.bat  
echo - Cả hai cùng lúc: start-all.bat
echo.

pause