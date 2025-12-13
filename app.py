from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, url_for, Response
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import (
    RoundedModuleDrawer,
    CircleModuleDrawer,
    GappedSquareModuleDrawer,
    SquareModuleDrawer,
    HorizontalBarsDrawer,
    VerticalBarsDrawer,
)
from qrcode.image.styles.colormasks import SolidFillColorMask
from qrcode.exceptions import DataOverflowError
import qrcode
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageChops
import io
import base64
import os
import sqlite3
from typing import Any
from typing import Optional

import re
import html


def sanitize_input(value: str, max_length: int = 2000) -> str:
    """Sanitize user input to prevent XSS and limit length."""
    if not value:
        return ''
    # Escape HTML entities
    sanitized = html.escape(str(value)[:max_length])
    return sanitized


def validate_hex_color(color: str) -> str:
    """Validate and return a safe hex color."""
    if not color:
        return '#000000'
    color = color.strip().lstrip('#')
    if re.match(r'^[0-9a-fA-F]{6}$', color):
        return f'#{color}'
    return '#000000'


def validate_int(value, default: int, min_val: int = None, max_val: int = None) -> int:
    """Safely parse and clamp an integer."""
    try:
        result = int(value)
        if min_val is not None:
            result = max(min_val, result)
        if max_val is not None:
            result = min(max_val, result)
        return result
    except (TypeError, ValueError):
        return default


def validate_float(value, default: float, min_val: float = None, max_val: float = None) -> float:
    """Safely parse and clamp a float."""
    try:
        result = float(value)
        if min_val is not None:
            result = max(min_val, result)
        if max_val is not None:
            result = min(max_val, result)
        return result
    except (TypeError, ValueError):
        return default


app = Flask(
    __name__,
    template_folder='.',
    static_folder='static',
    static_url_path='/static',
)


@app.route('/location')
def location_viewer():
        """Simple location viewer for scanned QR codes.

        Renders an interactive map with a marker at the provided coordinates and
        overlays Vietnamese labels for Hoàng Sa / Trường Sa.
        """
        lat = validate_float(request.args.get('lat'), 21.0285, -90.0, 90.0)
        lng = validate_float(request.args.get('lng'), 105.8542, -180.0, 180.0)
        zoom = validate_int(request.args.get('z'), 18, 1, 19)

        # Render as inline HTML to keep deployment simple.
        # CSP already allows unpkg + OSM tiles via img-src.
        page = f"""<!doctype html>
<html lang=\"vi\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Vị trí đã chọn</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" />
    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
    <style>
        html, body {{ height: 100%; margin: 0; }}
        body {{ background:#1e1e2e; color:#fff; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
        .topbar {{ padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.12); display:flex; gap:10px; align-items:center; }}
        .title {{ font-weight: 700; font-size: 14px; }}
        .coords {{ font-size: 12px; color: rgba(255,255,255,0.7); }}
        #map {{ height: calc(100% - 44px); width: 100%; }}
        .sovereignty-label {{
            background: rgba(30,30,46,0.85);
            color: #fff;
            border: 1px solid rgba(255,255,255,0.18);
            border-radius: 10px;
            padding: 6px 8px;
            font-size: 12px;
            font-weight: 700;
            white-space: nowrap;
            box-shadow: 0 10px 18px rgba(0,0,0,0.35);
        }}
    </style>
</head>
<body>
    <div class=\"topbar\">
        <div class=\"title\">Vị trí đã chọn</div>
        <div class=\"coords\">{lat:.6f}, {lng:.6f}</div>
    </div>
    <div id=\"map\"></div>
    <script>
        (function() {{
            var lat = {lat:.6f};
            var lng = {lng:.6f};
            var zoom = {zoom:d};
            var map = L.map('map', {{ zoomControl: true }}).setView([lat, lng], zoom);

            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                maxZoom: 19,
                subdomains: 'abc',
                attribution: ''
            }}).addTo(map);

            var markerIcon = L.divIcon({{
                className: 'qrio-leaflet-marker',
                html: `\
                    <svg width=\"32\" height=\"32\" viewBox=\"0 0 32 32\" aria-hidden=\"true\">\
                        <path d=\"M16 31s10-9.2 10-17A10 10 0 0 0 6 14c0 7.8 10 17 10 17z\" fill=\"#7c3aed\"/>\
                        <circle cx=\"16\" cy=\"14\" r=\"4.2\" fill=\"#ffffff\" fill-opacity=\"0.95\"/>\
                        <circle cx=\"16\" cy=\"14\" r=\"2.4\" fill=\"#1e1e2e\" fill-opacity=\"0.25\"/>\
                    </svg>`,
                iconSize: [32, 32],
                iconAnchor: [16, 31]
            }});
            L.marker([lat, lng], {{ icon: markerIcon }}).addTo(map);

            function label(lat, lng, text) {{
                return L.marker([lat, lng], {{
                    interactive: false,
                    icon: L.divIcon({{ className: 'sovereignty-label', html: text, iconSize: null }})
                }}).addTo(map);
            }}
            // Approximate label points for visibility. This does not draw any disputed boundary lines.
            label(16.5000, 112.3000, 'Quần đảo Hoàng Sa');
            label(10.0000, 114.3500, 'Quần đảo Trường Sa');
        }})();
    </script>
    <style>
        .qrio-leaflet-marker {{ background: transparent; border: none; }}
        .qrio-leaflet-marker svg {{ display:block; filter: drop-shadow(0 6px 10px rgba(0,0,0,0.35)); }}
    </style>
</body>
</html>"""
        return Response(page, mimetype='text/html; charset=utf-8')

# Security headers - CSP, XSS protection, cache
@app.after_request
def add_security_headers(response):
    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob: https://unpkg.com https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org https://server.arcgisonline.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Cache static assets
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000'  # 1 year
    
    return response


# Sitemap and robots.txt
@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('static', 'sitemap.xml', mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt', mimetype='text/plain')

# Tạo thư mục lưu nếu chưa có
thu_muc_ma = Path("mã")
thu_muc_ma.mkdir(exist_ok=True)

def _hex_to_rgb(color: str):
    """Convert #RRGGBB or RRGGBB to RGB tuple."""
    c = color.strip().lstrip('#')
    if len(c) != 6:
        return (0, 0, 0)
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    return (r, g, b)


def _parse_version(value):
    """Clamp and validate QR version (1-12)."""
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(12, version))


def _clamp_logo_size_percent(ecc_level: str, value, default: int = 25) -> tuple[int, int, bool]:
    """Clamp logo size percent to a scan-friendly range by ECC.

    Returns (clamped, max_allowed, was_clamped).
    """
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = int(default)

    ecc = (ecc_level or 'H').upper()
    # Conservative caps to avoid breaking scan reliability.
    max_allowed = {
        'L': 14,
        'M': 18,
        'Q': 22,
        'H': 25,
    }.get(ecc, 25)
    clamped = max(10, min(max_allowed, requested))
    return clamped, max_allowed, (clamped != requested)


def get_module_drawer(style, dot_type, dot_scale=1.0, dot_gap=0.0):
    """Get module drawer with customization for scale and gap."""
    # Clamp inputs to safe ranges
    dot_scale = max(0.5, min(1.5, dot_scale))
    dot_gap = max(0.0, min(0.6, dot_gap))

    # Base shrink from scale (1.0 = normal size)
    base_shrink = max(0.35, min(1.0, dot_scale))
    # Gap reduces the visible body size
    gap_penalty = dot_gap * 0.8
    shrink = max(0.25, base_shrink - gap_penalty)

    legacy_drawers = {
        'rounded': lambda: RoundedModuleDrawer(),
        'dots': lambda: CircleModuleDrawer(),
        'square': lambda: SquareModuleDrawer(),
        'classy': lambda: GappedSquareModuleDrawer(),
        'classy-rounded': lambda: RoundedModuleDrawer(),
        'extra-rounded': lambda: RoundedModuleDrawer(),
    }

    if style == 'legacy':
        return legacy_drawers.get(dot_type, lambda: RoundedModuleDrawer())()

    # Use gapped drawer when gap > 0.05 to make effect obvious
    def gapped_or(drawer_fn):
        if dot_gap > 0.05:
            return GappedSquareModuleDrawer()
        return drawer_fn()

    drawer_map = {
        'square': lambda: gapped_or(SquareModuleDrawer),
        'rounded-square': lambda: gapped_or(lambda: RoundedModuleDrawer(radius_ratio=0.45)),
        'circle': lambda: CircleModuleDrawer(),
        'rounded-bar': lambda: gapped_or(lambda: RoundedModuleDrawer(radius_ratio=0.7)),
        'horizontal-bar': lambda: HorizontalBarsDrawer(vertical_shrink=shrink),
        'vertical-bar': lambda: VerticalBarsDrawer(horizontal_shrink=shrink),
        'capsule': lambda: gapped_or(lambda: RoundedModuleDrawer(radius_ratio=1.0)),
        'organic': lambda: gapped_or(lambda: RoundedModuleDrawer(radius_ratio=0.95)),
    }
    return drawer_map.get(style, lambda: RoundedModuleDrawer(radius_ratio=0.45))()


def draw_shape(draw, x, y, size, fill, shape):
    if shape == 'square':
        draw.rectangle((x, y, x + size, y + size), fill=fill)
    elif shape == 'rounded-square':
        radius = max(2, size // 4)
        draw.rounded_rectangle((x, y, x + size, y + size), radius=radius, fill=fill)
    elif shape == 'circle':
        draw.ellipse((x, y, x + size, y + size), fill=fill)
    elif shape == 'diamond':
        cx = x + size / 2
        cy = y + size / 2
        points = [
            (cx, y),
            (x + size, cy),
            (cx, y + size),
            (x, cy),
        ]
        draw.polygon(points, fill=fill)
    else:
        draw.rectangle((x, y, x + size, y + size), fill=fill)


def draw_finder_pattern(draw, x, y, module_size, front_color, back_color, eye_style='square', eye_thickness=1.0):
    """Draw a single finder pattern (eye) with custom style."""
    # Finder pattern is 7x7 modules
    outer_size = 7 * module_size
    
    # eye_thickness: 0.7-2.0 controls the ring/border thickness
    # Higher value = thicker border ring
    t = max(0.7, min(2.0, eye_thickness))
    
    # Ring thickness in pixels (border between outer and middle)
    ring_width = max(1, int(module_size * t))
    
    # Calculate middle (gap) area - this is the "white" ring
    middle_offset = ring_width
    middle_size = outer_size - 2 * ring_width
    
    # Inner square is always 3x3 modules, centered
    inner_size = 3 * module_size
    inner_offset = (outer_size - inner_size) // 2
    
    # Ensure valid dimensions
    if middle_size < inner_size + 2:
        middle_size = inner_size + 2
        middle_offset = (outer_size - middle_size) // 2
    
    if eye_style == 'square':
        # Classic square style
        draw.rectangle((x, y, x + outer_size, y + outer_size), fill=front_color)
        draw.rectangle((x + middle_offset, y + middle_offset, 
                        x + middle_offset + middle_size, y + middle_offset + middle_size), fill=back_color)
        draw.rectangle((x + inner_offset, y + inner_offset,
                        x + inner_offset + inner_size, y + inner_offset + inner_size), fill=front_color)
    
    elif eye_style == 'rounded':
        # Rounded corners
        radius_outer = max(2, outer_size // 5)
        radius_middle = max(2, middle_size // 5)
        radius_inner = max(2, inner_size // 5)
        draw.rounded_rectangle((x, y, x + outer_size, y + outer_size), radius=radius_outer, fill=front_color)
        draw.rounded_rectangle((x + middle_offset, y + middle_offset,
                                x + middle_offset + middle_size, y + middle_offset + middle_size), 
                               radius=radius_middle, fill=back_color)
        draw.rounded_rectangle((x + inner_offset, y + inner_offset,
                                x + inner_offset + inner_size, y + inner_offset + inner_size),
                               radius=radius_inner, fill=front_color)
    
    elif eye_style == 'circle':
        # Bubble/Dot style
        draw.ellipse((x, y, x + outer_size, y + outer_size), fill=front_color)
        draw.ellipse((x + middle_offset, y + middle_offset,
                     x + middle_offset + middle_size, y + middle_offset + middle_size), fill=back_color)
        draw.ellipse((x + inner_offset, y + inner_offset,
                     x + inner_offset + inner_size, y + inner_offset + inner_size), fill=front_color)
    
    elif eye_style == 'rounded-bar':
        # Rounded bars style
        radius_outer = outer_size // 3
        radius_middle = middle_size // 3
        radius_inner = inner_size // 3
        draw.rounded_rectangle((x, y, x + outer_size, y + outer_size), radius=radius_outer, fill=front_color)
        draw.rounded_rectangle((x + middle_offset, y + middle_offset,
                                x + middle_offset + middle_size, y + middle_offset + middle_size),
                               radius=radius_middle, fill=back_color)
        draw.rounded_rectangle((x + inner_offset, y + inner_offset,
                                x + inner_offset + inner_size, y + inner_offset + inner_size),
                               radius=radius_inner, fill=front_color)
    
    elif eye_style == 'diamond':
        # Diamond style
        def draw_diamond(x1, y1, size, fill):
            cx = x1 + size / 2
            cy = y1 + size / 2
            half = size / 2
            points = [(cx, y1), (x1 + size, cy), (cx, y1 + size), (x1, cy)]
            draw.polygon(points, fill=fill)
        
        draw_diamond(x, y, outer_size, front_color)
        draw_diamond(x + middle_offset, y + middle_offset, middle_size, back_color)
        draw_diamond(x + inner_offset, y + inner_offset, inner_size, front_color)
    
    else:
        # Default square
        draw.rectangle((x, y, x + outer_size, y + outer_size), fill=front_color)
        draw.rectangle((x + middle_offset, y + middle_offset,
                        x + middle_offset + middle_size, y + middle_offset + middle_size), fill=back_color)
        draw.rectangle((x + inner_offset, y + inner_offset,
                        x + inner_offset + inner_size, y + inner_offset + inner_size), fill=front_color)


def apply_eye_style(img, qr_version, box_size, border, front_color, back_color, eye_style, eye_thickness=1.0):
    """Apply custom eye style to QR image by redrawing finder patterns."""
    if eye_style == 'square' and eye_thickness == 1.0:
        # Default style with no thickness change, no need to redraw
        return img
    
    draw = ImageDraw.Draw(img)
    module_size = box_size
    
    # Finder pattern positions (top-left corner of each 7x7 pattern)
    # Position in modules, then convert to pixels
    border_px = border * module_size
    
    # Three finder patterns: top-left, top-right, bottom-left
    finder_positions = [
        (border_px, border_px),  # Top-left
        (img.width - border_px - 7 * module_size, border_px),  # Top-right  
        (border_px, img.height - border_px - 7 * module_size),  # Bottom-left
    ]
    
    for (px, py) in finder_positions:
        # Clear the area first with background
        draw.rectangle((px, py, px + 7 * module_size, py + 7 * module_size), fill=back_color)
        # Draw custom finder pattern with eye_thickness
        draw_finder_pattern(draw, px, py, module_size, front_color, back_color, eye_style, eye_thickness)
    
    return img


def generate_qr(
    data,
    qr_color,
    bg_color,
    box_size=10,
    dot_type='rounded',
    border=4,
    ecc_level='H',
    version=None,
    module_style='legacy',
    logo_data=None,
    eye_style='square',
    logo_size_percent=25,
    logo_radius=0,
    dot_scale=1.0,
    dot_gap=0.0,
    eye_thickness=1.0,
):
    """Tạo QR code với các tùy chỉnh"""
    try:
        # Scale box size by dot_scale to make dots visibly thicker/thinner
        effective_box_size = max(6, int(box_size * max(0.5, min(1.5, dot_scale))))

        ecc_map = {
            'L': qrcode.constants.ERROR_CORRECT_L,
            'M': qrcode.constants.ERROR_CORRECT_M,
            'Q': qrcode.constants.ERROR_CORRECT_Q,
            'H': qrcode.constants.ERROR_CORRECT_H,
        }
        ecc = ecc_map.get(ecc_level.upper(), qrcode.constants.ERROR_CORRECT_H)

        version_arg = version if isinstance(version, int) and 1 <= version <= 40 else None
        qr = qrcode.QRCode(
            version=version_arg,
            error_correction=ecc,
            box_size=effective_box_size,
            border=border,
        )
        qr.add_data(data)
        try:
            qr.make(fit=False if version_arg else True)
        except DataOverflowError:
            qr = qrcode.QRCode(
                version=None,
                error_correction=ecc,
                box_size=effective_box_size,
                border=border,
            )
            qr.add_data(data)
            qr.make(fit=True)

        front = _hex_to_rgb(qr_color)
        back = _hex_to_rgb(bg_color)

        if front == back:
            front = (0, 0, 0)
            back = (255, 255, 255)

        module_drawer = get_module_drawer(module_style, dot_type, dot_scale, dot_gap)

        img = qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=module_drawer,
            color_mask=SolidFillColorMask(
                front_color=front,
                back_color=back,
            ),
        ).convert("RGBA")

        # Apply custom eye style
        if eye_style and (eye_style != 'square' or eye_thickness != 1.0):
            actual_version = qr.version if qr.version else 1
            img = apply_eye_style(img, actual_version, effective_box_size, border, front, back, eye_style, eye_thickness)

        if logo_data:
            logo = Image.open(io.BytesIO(logo_data)).convert("RGBA")
            # Clamp again for safety if generate_qr is called from elsewhere
            safe_logo_percent, _, _ = _clamp_logo_size_percent(ecc_level, logo_size_percent, default=25)
            logo_size = int(min(img.size) * (safe_logo_percent / 100))
            logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
            # Logo rounding (0-50%) where 50% ~ circle
            try:
                logo_radius = float(logo_radius)
            except (TypeError, ValueError):
                logo_radius = 0
            logo_radius = max(0.0, min(50.0, logo_radius))
            radius_px = int((logo_size / 2) * (logo_radius / 100.0))
            radius_px = max(0, min(radius_px, logo_size // 2))

            alpha = logo.split()[3] if logo.mode == "RGBA" else None
            if radius_px > 0:
                rounded = Image.new("L", (logo_size, logo_size), 0)
                rd = ImageDraw.Draw(rounded)
                rd.rounded_rectangle((0, 0, logo_size, logo_size), radius=radius_px, fill=255)
                if alpha is None:
                    alpha = rounded
                else:
                    alpha = ImageChops.multiply(alpha, rounded)
                logo.putalpha(alpha)

            mask = alpha
            pos = ((img.size[0] - logo_size) // 2, (img.size[1] - logo_size) // 2)

            # Carve a small quiet-zone around the logo so it doesn't visually clash with nearby modules.
            # This is still "removing dots" (filling with background), but only with a subtle margin.
            pad_px = max(2, int(logo_size * 0.08))
            pad_px = min(pad_px, max(2, logo_size // 6))

            x0 = max(0, pos[0] - pad_px)
            y0 = max(0, pos[1] - pad_px)
            x1 = min(img.size[0], pos[0] + logo_size + pad_px)
            y1 = min(img.size[1], pos[1] + logo_size + pad_px)
            draw = ImageDraw.Draw(img)
            back_rgba = (back[0], back[1], back[2], 255)
            if radius_px > 0:
                hole_radius = min((x1 - x0) // 2, (y1 - y0) // 2, radius_px + pad_px)
                draw.rounded_rectangle((x0, y0, x1, y1), radius=hole_radius, fill=back_rgba)
            else:
                draw.rectangle((x0, y0, x1, y1), fill=back_rgba)

            img.paste(logo, pos, mask if mask else logo)

        return img
    except Exception as e:
        print(f"Lỗi tạo QR: {e}")
        return None


@app.route('/')
def index():
    """Serve trang chủ mặc định (home)."""
    return send_from_directory('.', 'home.html')


@app.route('/home.html')
def home_html():
    """Alias: truy cập trực tiếp home.html"""
    return send_from_directory('.', 'home.html')


@app.route('/edit')
@app.route('/edit.html')
def edit_html():
    """Trang chỉnh sửa (tên cũ: index)."""
    return send_from_directory('.', 'edit.html')


@app.route('/index.html')
def legacy_index_html():
    """Giữ tương thích cũ: /index.html trỏ về edit.html"""
    return send_from_directory('.', 'edit.html')



@app.route('/favicon.ico')
def favicon():
    """Serve favicon (browsers often request /favicon.ico even if link tags exist)."""
    return redirect(url_for('static', filename='logo/favicon_io (3)/favicon.ico'))


# ===== LOCAL MBTILES TILE SERVER (offline, same-origin for CSP) =====

def _guess_tile_mime(tile_bytes: bytes) -> str:
    if not tile_bytes:
        return 'application/octet-stream'
    # PNG signature
    if tile_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    # JPEG
    if tile_bytes.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    # WEBP (RIFF....WEBP)
    if len(tile_bytes) >= 12 and tile_bytes[0:4] == b'RIFF' and tile_bytes[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def _find_mbtiles_path() -> Optional[Path]:
    env = os.environ.get('MBTILES_PATH') or os.environ.get('MAP_MBTILES_PATH')
    if env:
        p = Path(env)
        if p.exists() and p.is_file():
            return p

    # Conventional locations
    candidates = [
        Path('tiles') / 'map.mbtiles',
        Path('tiles') / 'tiles.mbtiles',
        Path('static') / 'tiles.mbtiles',
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c

    # First .mbtiles in ./tiles
    tiles_dir = Path('tiles')
    if tiles_dir.exists() and tiles_dir.is_dir():
        for f in tiles_dir.glob('*.mbtiles'):
            if f.is_file():
                return f

    # Also allow dropping a single .mbtiles next to app.py
    for f in Path('.').glob('*.mbtiles'):
        if f.is_file():
            return f

    return None


def _get_mbtiles_conn(path: Path) -> sqlite3.Connection:
    # Read-only open (uri mode) to avoid locking issues
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    return conn


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _read_metadata(conn: sqlite3.Connection) -> dict:
    if not _has_table(conn, 'metadata'):
        return {}
    out: dict[str, str] = {}
    try:
        for k, v in conn.execute('SELECT name, value FROM metadata').fetchall():
            if isinstance(k, str):
                out[k] = v
    except Exception:
        return out
    return out


def _query_one_tile(conn: sqlite3.Connection, z: int, x: int, y: int):
    return conn.execute(
        'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1',
        (z, x, y),
    ).fetchone()


def _query_one_tile_map_images(conn: sqlite3.Connection, z: int, x: int, y: int):
    # Some MBTiles producers store de-duplicated tiles in `images` and an index in `map`.
    return conn.execute(
        'SELECT images.tile_data '
        'FROM map JOIN images ON map.tile_id = images.tile_id '
        'WHERE map.zoom_level=? AND map.tile_column=? AND map.tile_row=? '
        'LIMIT 1',
        (z, x, y),
    ).fetchone()


def _mbtiles_zoom_range(conn: sqlite3.Connection):
    row = conn.execute('SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles').fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _mbtiles_zoom_range_map_images(conn: sqlite3.Connection):
    row = conn.execute('SELECT MIN(zoom_level), MAX(zoom_level) FROM map').fetchone()
    if not row:
        return None, None
    return row[0], row[1]


@app.route('/tiles/status')
def mbtiles_status():
    """Return whether a local MBTiles file is available and what it contains."""
    searched = [
        'MBTILES_PATH / MAP_MBTILES_PATH (env)',
        'tiles/map.mbtiles',
        'tiles/tiles.mbtiles',
        'static/tiles.mbtiles',
        'tiles/*.mbtiles (first match)',
    ]

    mbtiles_path = _find_mbtiles_path()
    if not mbtiles_path:
        return jsonify({
            'found': False,
            'path': None,
            'searched': searched,
            'hint': 'Put a *raster* .mbtiles file at tiles/map.mbtiles (or set MBTILES_PATH).',
        })

    info: dict[str, Any] = {
        'found': True,
        'path': str(mbtiles_path),
        'searched': searched,
        'schema': None,
        'min_zoom': None,
        'max_zoom': None,
        'sample_mime': None,
        'is_raster': None,
        'metadata': None,
    }

    try:
        conn = _get_mbtiles_conn(mbtiles_path)
        try:
            md = _read_metadata(conn)
            info['metadata'] = {k: md.get(k) for k in ('name', 'format', 'bounds', 'center', 'minzoom', 'maxzoom') if k in md}

            if _has_table(conn, 'tiles'):
                info['schema'] = 'tiles'
                min_z, max_z = _mbtiles_zoom_range(conn)
                info['min_zoom'] = min_z
                info['max_zoom'] = max_z
                sample = conn.execute('SELECT tile_data FROM tiles LIMIT 1').fetchone()
                if sample and sample[0]:
                    mime = _guess_tile_mime(sample[0])
                    info['sample_mime'] = mime
                    info['is_raster'] = bool(mime.startswith('image/'))
            elif _has_table(conn, 'map') and _has_table(conn, 'images'):
                info['schema'] = 'map_images'
                min_z, max_z = _mbtiles_zoom_range_map_images(conn)
                info['min_zoom'] = min_z
                info['max_zoom'] = max_z
                sample = conn.execute('SELECT tile_data FROM images LIMIT 1').fetchone()
                if sample and sample[0]:
                    mime = _guess_tile_mime(sample[0])
                    info['sample_mime'] = mime
                    info['is_raster'] = bool(mime.startswith('image/'))
            else:
                info['schema'] = 'unknown'
        finally:
            conn.close()
    except Exception as e:
        info['error'] = str(e)

    return jsonify(info)


@app.route('/tiles/<int:z>/<int:x>/<int:y>.png')
def mbtiles_tile_png(z: int, x: int, y: int):
    """Serve XYZ raster tiles from a local MBTiles file.

    Expected MBTiles schema: table `tiles` with columns
    (zoom_level, tile_column, tile_row, tile_data).
    MBTiles uses TMS Y, so we flip from XYZ.
    """
    if z < 0 or z > 22:
        return Response(status=404)

    max_index = (1 << z) - 1
    if x < 0 or y < 0 or x > max_index or y > max_index:
        return Response(status=404)

    mbtiles_path = _find_mbtiles_path()
    if not mbtiles_path:
        return Response(status=404)

    tms_y = max_index - y

    try:
        conn = _get_mbtiles_conn(mbtiles_path)
        try:
            row = None
            if _has_table(conn, 'tiles'):
                # MBTiles spec uses TMS Y, but some generators store XYZ Y.
                # Try TMS first, then fall back to XYZ for compatibility.
                row = _query_one_tile(conn, z, x, tms_y)
                if not row and tms_y != y:
                    row = _query_one_tile(conn, z, x, y)
            elif _has_table(conn, 'map') and _has_table(conn, 'images'):
                row = _query_one_tile_map_images(conn, z, x, tms_y)
                if not row and tms_y != y:
                    row = _query_one_tile_map_images(conn, z, x, y)
            else:
                row = None
        finally:
            conn.close()
    except Exception as e:
        print(f"MBTiles error: {e}")
        return Response(status=500)

    if not row:
        return Response(status=404)

    tile_bytes = row[0]
    mime = _guess_tile_mime(tile_bytes)
    resp = Response(tile_bytes, mimetype=mime)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/generate', methods=['POST'])
def api_generate():
    """API tạo QR preview"""
    try:
        # Sanitize and validate all inputs
        data = sanitize_input(request.form.get('data', 'https://qrio.vn'), max_length=4000)
        if not data:
            data = 'https://qrio.vn'
        
        qr_color = validate_hex_color(request.form.get('qr_color', '#0c6c3b'))
        bg_color = validate_hex_color(request.form.get('bg_color', '#ffffff'))
        box_size = validate_int(request.form.get('box_size', 10), default=10, min_val=1, max_val=50)
        dot_type = sanitize_input(request.form.get('dot_type', 'rounded'), max_length=50)
        border = validate_int(request.form.get('border', 4), default=4, min_val=0, max_val=20)
        ecc_level = sanitize_input(request.form.get('ecc_level', 'H'), max_length=1).upper()
        if ecc_level not in ('L', 'M', 'Q', 'H'):
            ecc_level = 'H'
        version = _parse_version(request.form.get('version'))
        module_style = sanitize_input(request.form.get('module_style', 'legacy'), max_length=50)
        eye_style = sanitize_input(request.form.get('eye_style', 'square'), max_length=50)
        requested_logo_size = request.form.get('logo_size', 25)
        logo_size_percent, logo_size_max, logo_size_clamped = _clamp_logo_size_percent(ecc_level, requested_logo_size, default=25)
        logo_radius = validate_int(request.form.get('logo_radius', 0), default=0, min_val=0, max_val=50)
        dot_scale = validate_float(request.form.get('dot_scale', 1.0), default=1.0, min_val=0.5, max_val=1.5)
        dot_gap = validate_float(request.form.get('dot_gap', 0.0), default=0.0, min_val=0.0, max_val=0.6)
        eye_thickness = validate_float(request.form.get('eye_thickness', 1.0), default=1.0, min_val=0.7, max_val=2.0)
        
        # Logo (nếu có) - validate file size (max 2MB)
        logo_data = None
        if 'logo' in request.files and request.files['logo'].filename:
            logo_file = request.files['logo']
            logo_data = logo_file.read()
            if len(logo_data) > 2 * 1024 * 1024:  # 2MB limit
                return jsonify({'error': 'Logo quá lớn (tối đa 2MB)'}), 400
        
        img = generate_qr(
            data,
            qr_color,
            bg_color,
            box_size,
            dot_type,
            border,
            ecc_level,
            version,
            module_style,
            logo_data,
            eye_style,
            logo_size_percent,
            logo_radius,
            dot_scale,
            dot_gap,
            eye_thickness,
        )
        
        if not img:
            return jsonify({'error': 'Không thể tạo QR'}), 400
        
        # Chuyển thành base64 để gửi lại frontend
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.getvalue()).decode()
        
        payload = {'image': f'data:image/png;base64,{img_base64}'}
        if logo_size_clamped:
            payload.update({
                'logo_size_used': logo_size_percent,
                'logo_size_max': logo_size_max,
                'logo_size_clamped': True,
            })
        return jsonify(payload)
    except Exception as e:
        print(f"Error in api_generate: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    """API tải QR dưới dạng file (có thể kèm tiêu đề)"""
    try:
        data = request.form.get('data', 'https://agrichat.site')
        qr_color = request.form.get('qr_color', '#0c6c3b')
        bg_color = request.form.get('bg_color', '#ffffff')
        box_size = int(request.form.get('box_size', 10))
        dot_type = request.form.get('dot_type', 'rounded')
        border = int(request.form.get('border', 4))
        ecc_level = request.form.get('ecc_level', 'H')
        version = _parse_version(request.form.get('version'))
        module_style = request.form.get('module_style', 'legacy')
        eye_style = request.form.get('eye_style', 'square')
        filename = request.form.get('filename', 'qr_code')

        # Logo rounding (optional)
        logo_radius = request.form.get('logo_radius', 0)
        
        # Title options
        title_top = request.form.get('title_top', '')
        title_bottom = request.form.get('title_bottom', '')
        title_color = request.form.get('title_color', '#1e293b')
        title_top_size = int(request.form.get('title_top_size', 18))
        title_bottom_size = int(request.form.get('title_bottom_size', 14))
        
        # Logo (nếu có)
        logo_data = None
        if 'logo' in request.files and request.files['logo'].filename:
            logo_data = request.files['logo'].read()
        
        qr_img = generate_qr(
            data,
            qr_color,
            bg_color,
            box_size,
            dot_type,
            border,
            ecc_level,
            version,
            module_style,
            logo_data,
            eye_style,
            25,
            logo_radius,
        )
        
        if not qr_img:
            return jsonify({'error': 'Không thể tạo QR'}), 400
        
        # Nếu có tiêu đề, render vào ảnh
        if title_top or title_bottom:
            qr_img = add_titles_to_qr(qr_img, title_top, title_bottom, title_color, title_top_size, title_bottom_size, bg_color)
        
        # Lưu file
        buf = io.BytesIO()
        qr_img.save(buf, format='PNG')
        buf.seek(0)
        
        # Lưu vào thư mục mã
        file_path = thu_muc_ma / f"{filename}.png"
        qr_img.save(str(file_path))
        
        return send_file(buf, mimetype='image/png', as_attachment=True, download_name=f'{filename}.png')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def add_titles_to_qr(qr_img, title_top, title_bottom, title_color, top_size, bottom_size, bg_color):
    """Thêm tiêu đề vào ảnh QR"""
    try:
        # Tìm font
        font_paths = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc"
        ]
        
        font_path = None
        for fp in font_paths:
            if os.path.exists(fp):
                font_path = fp
                break
        
        # Load fonts
        if font_path:
            font_top = ImageFont.truetype(font_path, top_size * 2)  # Scale up for better quality
            font_bottom = ImageFont.truetype(font_path, bottom_size * 2)
        else:
            font_top = ImageFont.load_default()
            font_bottom = ImageFont.load_default()
        
        # Calculate dimensions
        padding = 40
        qr_width, qr_height = qr_img.size
        
        # Measure text
        dummy_draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        
        top_height = 0
        if title_top:
            bbox = dummy_draw.textbbox((0, 0), title_top, font=font_top)
            top_height = bbox[3] - bbox[1] + 30
        
        bottom_height = 0
        if title_bottom:
            bbox = dummy_draw.textbbox((0, 0), title_bottom, font=font_bottom)
            bottom_height = bbox[3] - bbox[1] + 30
        
        # Create new image
        new_width = qr_width + padding * 2
        new_height = qr_height + padding * 2 + top_height + bottom_height
        
        new_img = Image.new('RGBA', (new_width, new_height), bg_color)
        draw = ImageDraw.Draw(new_img)
        
        # Draw top title
        if title_top:
            bbox = draw.textbbox((0, 0), title_top, font=font_top)
            text_width = bbox[2] - bbox[0]
            x = (new_width - text_width) // 2
            draw.text((x, padding), title_top, fill=title_color, font=font_top)
        
        # Paste QR
        new_img.paste(qr_img, (padding, padding + top_height))
        
        # Draw bottom title
        if title_bottom:
            bbox = draw.textbbox((0, 0), title_bottom, font=font_bottom)
            text_width = bbox[2] - bbox[0]
            x = (new_width - text_width) // 2
            y = padding + top_height + qr_height + 15
            draw.text((x, y), title_bottom, fill=title_color, font=font_bottom)
        
        return new_img
    except Exception as e:
        print(f"Lỗi thêm tiêu đề: {e}")
        return qr_img


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') == 'development'
    host = '0.0.0.0' if not debug else 'localhost'
    print(f"✓ Qrio đang chạy tại http://localhost:{port}")
    print("✓ Mở trình duyệt và truy cập")
    print("✓ Bấn Ctrl+C để dừng server")
    app.run(debug=debug, host=host, port=port)
