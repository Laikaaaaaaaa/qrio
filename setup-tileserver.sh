# TileServer GL và OpenMapTiles Setup Script

# 1. Cài đặt TileServer GL
npm install -g tileserver-gl-light

# 2. Download OpenMapTiles data cho Vietnam
# Lưu ý: OpenMapTiles có thể yêu cầu API key cho download
# Hoặc có thể tự tạo từ planet.osm data bằng OpenMapTiles tools

# Option 1: Download từ OpenMapTiles (cần key)
# wget -O data/vietnam.mbtiles "https://api.maptiler.com/tiles/v3/{api_key}/vietnam.mbtiles"

# Option 2: Build từ OpenStreetMap data (free)
echo "Tạo folder workspace cho OpenMapTiles..."
mkdir openmaptiles-workspace
cd openmaptiles-workspace

# Download OpenMapTiles tools
echo "Clone OpenMapTiles tools..."
git clone https://github.com/openmaptiles/openmaptiles.git
cd openmaptiles

# Vietnam bounds: 102.0,5.0,118.8,25.2 (west,south,east,north)
echo "Tạo .env file với bounds Vietnam..."
cat > .env << EOL
BBOX=102.0,5.0,118.8,25.2
MIN_ZOOM=0
MAX_ZOOM=14
EXPORT_FILE_FORMAT=mbtiles
EXPORT_FILE_NAME=vietnam
OSM_AREA_NAME=vietnam
DIFF_MODE=false
QUICKSTART=true
EOL

# Docker setup cần thiết
echo "Setup Docker environment cho OpenMapTiles..."
echo "Chạy lệnh sau để build tiles từ OSM data:"
echo "make"

# Copy kết quả về project chính
echo "Sau khi build xong, copy file:"
echo "cp data/vietnam.mbtiles ../../../data/"

cd ../../..

echo "Setup hoàn thành!"
echo ""
echo "Để chạy TileServer GL:"
echo "tileserver-gl-light --config tileserver-config.json --port 8080"
echo ""
echo "Sau đó update app.py để proxy vector tiles từ localhost:8080"