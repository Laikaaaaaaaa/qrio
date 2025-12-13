# Qrio - QR Code Editor với Vector Tiles tự host

## Tổng quan
Stack hoàn chỉnh cho QR editor với bản đồ Việt Nam sử dụng:
- **OpenMapTiles** (world data) 
- **TileServer GL** (host local vector tiles)
- **MapLibre GL JS** (frontend rendering)
- **Flask** (backend + fallback Leaflet)

## Yêu cầu hệ thống
- Node.js 16+
- Python 3.8+
- 4GB RAM (để xử lý vector tiles)
- Khoảng 2GB dung lượng (cho Vietnam tiles)

## Cài đặt nhanh

### 1. Setup môi trường
```bash
# Chạy script tự động
setup.bat

# Hoặc cài đặt thủ công:
npm install -g tileserver-gl-light
pip install -r requirements.txt
```

### 2. Download OpenMapTiles Vietnam
**Option A: Tải từ OpenMapTiles**
```bash
# Truy cập https://openmaptiles.org/downloads/vietnam/
# Tải file vietnam.mbtiles về data/vietnam.mbtiles
```

**Option B: Tự build từ OSM data (free)**
```bash
git clone https://github.com/openmaptiles/openmaptiles.git
cd openmaptiles

# Tạo .env file với bounds Vietnam
echo "BBOX=102.0,5.0,118.8,25.2" > .env
echo "MIN_ZOOM=0" >> .env  
echo "MAX_ZOOM=14" >> .env
echo "OSM_AREA_NAME=vietnam" >> .env

# Build (cần Docker)
make
cp data/vietnam.mbtiles ../data/
```

### 3. Khởi động services

**Option A: Tự động (khuyến nghị)**
```bash
start-all.bat
```

**Option B: Thủ công**
```bash
# Terminal 1: TileServer GL
tileserver-gl-light --config tileserver-config.json --port 8080

# Terminal 2: Flask app  
python app.py
```

## URLs truy cập
- **QR Editor**: http://localhost:5000/edit
- **TileServer GL**: http://localhost:8080 
- **Vector tile status**: http://localhost:5000/api/tileserver/status

## Kiến trúc hệ thống

### Luồng dữ liệu
```
OpenMapTiles → TileServer GL → Flask proxy → MapLibre GL JS → Frontend
vietnam.mbtiles   :8080         :5000/api     frontend         UI
```

### Fallback logic
1. **Vector tiles available**: MapLibre GL + Vietnam style + Vietnamese labels
2. **Vector tiles unavailable**: Leaflet + MBTiles raster + overlay labels  
3. **No tiles**: Leaflet + neutral grid + overlay labels

### Vietnam-specific features
- **Bounds giới hạn**: `102.0,5.0,118.8,25.2` (bao gồm Hoàng Sa, Trường Sa)
- **Vietnamese labels**: "Biển Đông", "Quần đảo Hoàng Sa", "Quần đảo Trường Sa"
- **No external tiles**: Hoàn toàn self-hosted, tránh "South China Sea"

## Cấu hình files

### tileserver-config.json
```json
{
  "options": {
    "paths": {
      "styles": "styles",
      "fonts": "fonts",
      "mbtiles": "data"
    }
  },
  "styles": {
    "vietnam": {
      "style": "vietnam.json",
      "tilejson": {
        "bounds": [102.0, 5.0, 118.8, 25.2]
      }
    }
  },
  "data": {
    "vietnam": {
      "mbtiles": "vietnam.mbtiles"
    }
  }
}
```

### styles/vietnam.json
- Vietnamese place names priority: `{name:vi}`
- Custom water labels: "Biển Đông" replaces "South China Sea"
- Island labels: Hoàng Sa, Trường Sa với Vietnamese names
- Vietnam-focused zoom levels và styling

### app.py
- `/api/tiles/*`: Proxy to TileServer GL  
- `/api/tileserver/status`: Health check
- CSP headers: Chỉ cho phép self + unpkg/cdnjs
- Location QR: `/location?lat=...&lng=...` với Vietnamese labels

### edit.html
- **Hybrid map system**: Auto-detect vector tiles availability
- **MapLibre integration**: Với Vietnamese style  
- **Leaflet fallback**: MBTiles raster + grid
- **Vietnamese UI**: Tất cả text interface tiếng Việt

## API Endpoints

### TileServer GL proxy
```
GET /api/tiles/styles/vietnam.json     # Style definition
GET /api/tiles/data/vietnam.json       # TileJSON metadata  
GET /api/tiles/data/vietnam/{z}/{x}/{y}.pbf  # Vector tiles
GET /api/tileserver/status             # Health check
```

### QR & Location
```
POST /generate                         # Generate QR code
GET /location?lat=...&lng=...         # Location viewer với map
```

## Troubleshooting

### TileServer GL không start
```bash
# Kiểm tra port conflicts
netstat -an | findstr :8080

# Kiểm tra config
tileserver-gl-light --help
```

### Tiles không load
1. Kiểm tra `data/vietnam.mbtiles` tồn tại
2. Kiểm tra TileServer GL đang chạy: http://localhost:8080
3. Xem browser console để check lỗi CORS/CSP

### Map hiển thị trắng
1. Fallback sẽ tự động chuyển sang Leaflet + MBTiles raster
2. Nếu vẫn trắng: grid overlay sẽ hiển thị
3. Kiểm tra browser devtools Network tab

### Performance optimization
```bash
# Giảm zoom levels để file nhỏ hơn
MAX_ZOOM=12 make

# Optimize MBTiles
mb-util vietnam.mbtiles vietnam_tiles --image_format=webp
```

## Deployment Production

### Heroku
```bash
# Add TileServer GL buildpack
heroku buildpacks:add heroku/nodejs
heroku buildpacks:add heroku/python

# Config vars
heroku config:set NODE_ENV=production
heroku config:set FLASK_ENV=production

git push heroku main
```

### Docker
```dockerfile
FROM node:16-alpine
WORKDIR /app
COPY . .
RUN npm install -g tileserver-gl-light
RUN pip install -r requirements.txt
EXPOSE 5000 8080
CMD ["start-all.bat"]
```

## Security & Compliance

### CSP Policy
```
self, unpkg.com, cdnjs.cloudflare.com
```

### Vietnamese Sovereignty
- ❌ Không có external tiles với "South China Sea"  
- ✅ Chỉ Vietnamese labels: "Biển Đông", "Hoàng Sa", "Trường Sa"
- ✅ Vietnam bounds chính xác bao gồm EEZ
- ✅ Hoàn toàn self-hosted, không phụ thuộc external services

## Monitoring & Logs
```bash
# TileServer GL metrics
curl http://localhost:8080/health

# Flask app status  
curl http://localhost:5000/api/tileserver/status

# Tile requests
tail -f logs/access.log
```