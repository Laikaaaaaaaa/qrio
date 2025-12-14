from flask import Flask, request, jsonify, send_file, send_from_directory, redirect, url_for, Response
from werkzeug.utils import secure_filename
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
import uuid
import mimetypes
from typing import Any
from typing import Optional
import json
import requests
import time

import re


_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')


def sanitize_input(value: str, max_length: int = 2000) -> str:
    """Sanitize user input for server-side processing.

    Note: Do NOT HTML-escape here because the values are not rendered into HTML.
    Escaping would corrupt QR payloads (e.g. URLs with '&', mailto params, etc.).
    """
    if value is None:
        return ''
    s = str(value)[:max_length]
    s = _CONTROL_CHARS_RE.sub('', s)
    return s.strip()


def sanitize_qr_data(value: str, max_length: int = 4000) -> str:
    """Sanitize QR payload without altering its semantics."""
    if value is None:
        return ''
    s = str(value)[:max_length]
    # Remove only control chars that can break generators/scanners.
    return _CONTROL_CHARS_RE.sub('', s)


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


def _hex_to_rgb(color: str):
    """Convert #RRGGBB or RRGGBB to an (R, G, B) tuple."""
    if not color:
        return (0, 0, 0)
    c = str(color).strip().lstrip('#')
    if len(c) != 6 or not re.match(r'^[0-9a-fA-F]{6}$', c):
        return (0, 0, 0)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _parse_version(value):
    """Parse QR version. Returns int 1..40 or None for auto-fit."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(40, v))


def _clamp_logo_size_percent(ecc_level: str, requested, default: float = 25.0):
    """Clamp logo size percent based on ECC level.

    Returns (percent_used, max_allowed, clamped_bool).
    """
    try:
        pct = float(requested)
    except (TypeError, ValueError):
        pct = float(default)

    ecc = (ecc_level or 'H').upper()
    max_by_ecc = {
        'L': 18.0,
        'M': 22.0,
        'Q': 26.0,
        'H': 30.0,
    }
    max_allowed = max_by_ecc.get(ecc, 30.0)
    pct_clamped = max(5.0, min(max_allowed, pct))
    return pct_clamped, max_allowed, (pct_clamped != pct)


def get_module_drawer(module_style: str, dot_type: str, dot_scale: float = 1.0, dot_gap: float = 0.0):
    """Return a qrcode module drawer based on style selection."""
    style = (module_style or '').lower().strip()
    dt = (dot_type or '').lower().strip()

    if style in ('legacy', ''):
        # Keep legacy behavior driven by dot_type.
        if dt in ('circle', 'dot', 'dots'):
            return CircleModuleDrawer()
        if dt in ('square',):
            return SquareModuleDrawer()
        if dt in ('gapped', 'gap'):
            return GappedSquareModuleDrawer()
        if dt in ('hbars', 'horizontal-bars'):
            return HorizontalBarsDrawer()
        if dt in ('vbars', 'vertical-bars'):
            return VerticalBarsDrawer()
        return RoundedModuleDrawer()

    if style in ('rounded', 'round'):
        return RoundedModuleDrawer()
    if style in ('circle', 'dot', 'dots'):
        return CircleModuleDrawer()
    if style in ('square',):
        return SquareModuleDrawer()
    if style in ('gapped', 'gap'):
        return GappedSquareModuleDrawer()
    if style in ('hbars', 'horizontal-bars'):
        return HorizontalBarsDrawer()
    if style in ('vbars', 'vertical-bars'):
        return VerticalBarsDrawer()

    return RoundedModuleDrawer()


app = Flask(
    __name__,
    template_folder='.',
    static_folder='static',
    static_url_path='/static',
)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# ========================
# ADMIN DASHBOARD INTEGRATION
# ========================
from admin import admin_bp, analytics_bp, track_event

app.register_blueprint(admin_bp)
app.register_blueprint(analytics_bp)


# Security headers - CSP, XSS protection, cache
@app.after_request
def add_security_headers(response):
    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
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


def draw_finder_pattern(draw, x, y, module_size, front_color, back_color, eye_style, eye_thickness=1.0):
    """Draw a 7x7 finder pattern at (x, y) with configurable eye style/thickness."""
    outer_size = 7 * module_size
    t = float(eye_thickness) if eye_thickness is not None else 1.0

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


# ========================
# ANALYTICS TRACKING HELPER
# ========================
_GEOIP_CACHE = {}  # ip -> (country_code, ts)
_GEOIP_CACHE_TTL = 3600  # seconds


def _normalize_country_code(value: str) -> str:
    code = (value or '').strip().upper()
    if not code:
        return 'Unknown'
    if code in ('UNKNOWN', 'UN', 'N/A', 'NA', 'NONE', 'NULL', 'XX', 'ZZ'):
        return 'Unknown'
    if re.match(r'^[A-Z]{2}$', code):
        return code
    return 'Unknown'


def _get_client_ip_for_geo() -> Optional[str]:
    def _is_public_ip(value: str) -> bool:
        try:
            import ipaddress

            addr = ipaddress.ip_address(value)
            if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
                return False
            # Covers RFC1918, CGNAT, unique-local v6, etc.
            if addr.is_private:
                return False
            # Some Python versions expose is_reserved; keep safe
            if getattr(addr, 'is_reserved', False):
                return False
            return True
        except Exception:
            return False

    # Common direct client IP headers (CDNs / proxies)
    direct_headers = (
        'CF-Connecting-IP',
        'True-Client-IP',
        'X-Real-IP',
    )
    for h in direct_headers:
        raw = (request.headers.get(h, '') or '').strip()
        if raw and _is_public_ip(raw):
            return raw

    # Standard proxy chain
    forwarded = (request.headers.get('X-Forwarded-For', '') or '').strip()
    if forwarded:
        parts = [p.strip() for p in forwarded.split(',') if p.strip()]
        for candidate in parts:
            if _is_public_ip(candidate):
                return candidate

    # Fallback to remote_addr
    ip = (request.remote_addr or '').strip()
    if ip and _is_public_ip(ip):
        return ip
    return None


def _geoip_lookup_country(ip: str) -> str:
    """Optional IP→country lookup. Disabled by default (privacy + external call)."""
    enable = (os.environ.get('ENABLE_GEOIP', '') or '').strip().lower()
    if enable not in ('1', 'true', 'yes', 'on'):
        return 'Unknown'

    now = time.time()
    cached = _GEOIP_CACHE.get(ip)
    if cached and (now - cached[1]) < _GEOIP_CACHE_TTL:
        return cached[0]

    try:
        provider = (os.environ.get('GEOIP_PROVIDER', 'auto') or 'auto').strip().lower()

        # Provider: ipapi.co (plain text ISO-2)
        if provider in ('auto', 'ipapi'):
            try:
                url = f'https://ipapi.co/{ip}/country/'
                r = requests.get(url, timeout=1.6, headers={'User-Agent': 'Qrio/1.0'})
                if r.status_code == 200:
                    code = _normalize_country_code(r.text)
                    if code != 'Unknown':
                        _GEOIP_CACHE[ip] = (code, now)
                        return code
            except Exception:
                pass

        # Provider: ipwho.is (JSON, no key)
        if provider in ('auto', 'ipwhois', 'ipwho'):
            try:
                url = f'https://ipwho.is/{ip}'
                r = requests.get(url, timeout=1.6, headers={'User-Agent': 'Qrio/1.0'})
                if r.status_code == 200:
                    data = r.json() if r.headers.get('Content-Type', '').lower().startswith('application/json') else None
                    if isinstance(data, dict):
                        code = _normalize_country_code(data.get('country_code') or '')
                        if code != 'Unknown':
                            _GEOIP_CACHE[ip] = (code, now)
                            return code
            except Exception:
                pass

        # Custom provider: GEOIP_URL_TEMPLATE like https://example.com/lookup?ip={ip}
        if provider not in ('auto', 'ipapi', 'ipwhois', 'ipwho'):
            tpl = (os.environ.get('GEOIP_URL_TEMPLATE') or '').strip()
            if tpl and '{ip}' in tpl:
                url = tpl.replace('{ip}', ip)
                r = requests.get(url, timeout=1.6, headers={'User-Agent': 'Qrio/1.0'})
                if r.status_code == 200:
                    code = _normalize_country_code(r.text)
                else:
                    code = 'Unknown'
            else:
                code = 'Unknown'
        else:
            code = 'Unknown'
    except Exception:
        code = 'Unknown'

    _GEOIP_CACHE[ip] = (code, now)
    return code


def get_country_from_request():
    """Get country from common CDN headers; fallback optional geoip."""
    # Prefer CDN-provided country headers (no external calls).
    candidates = (
        'CF-IPCountry',
        'CloudFront-Viewer-Country',
        'X-Vercel-IP-Country',
        'Fly-Client-Country',
        'Fastly-Client-Country',
        'X-AppEngine-Country',
    )
    for h in candidates:
        code = _normalize_country_code(request.headers.get(h, ''))
        if code != 'Unknown':
            return code

    ip = _get_client_ip_for_geo()
    if ip:
        return _geoip_lookup_country(ip)
    return 'Unknown'


def get_device_type():
    """Simple device detection from User-Agent."""
    ua = request.headers.get('User-Agent', '').lower()
    if 'mobile' in ua or 'android' in ua or 'iphone' in ua:
        return 'Mobile'
    if 'tablet' in ua or 'ipad' in ua:
        return 'Tablet'
    return 'Desktop'


@app.route('/')
def index():
    """Serve trang chủ mặc định (home)."""
    track_event('/', 'page_view', get_country_from_request(), get_device_type())
    return send_from_directory('.', 'home.html')


@app.route('/home.html')
def home_html():
    """Alias: truy cập trực tiếp home.html"""
    return send_from_directory('.', 'home.html')


@app.route('/edit')
@app.route('/edit.html')
def edit_html():
    """Trang chỉnh sửa (tên cũ: index)."""
    track_event('/edit', 'page_view', get_country_from_request(), get_device_type())
    return send_from_directory('.', 'edit.html')


@app.route('/index.html')
def legacy_index_html():
    """Giữ tương thích cũ: /index.html trỏ về edit.html"""
    return send_from_directory('.', 'edit.html')


@app.route('/terms')
@app.route('/terms.html')
def terms_html():
    """Legal: Terms of Service."""
    return send_from_directory('.', 'terms.html')


@app.route('/privacy')
@app.route('/privacy.html')
def privacy_html():
    """Legal: Privacy Policy."""
    return send_from_directory('.', 'privacy.html')


@app.route('/disclaimer')
@app.route('/disclaimer.html')
def disclaimer_html():
    """Legal: Disclaimer."""
    return send_from_directory('.', 'disclaimer.html')


@app.route('/about')
@app.route('/about.html')
def about_html():
    """About Us page."""
    return send_from_directory('.', 'about.html')


@app.route('/contact')
@app.route('/contact.html')
def contact_html():
    """Contact page."""
    return send_from_directory('.', 'contact.html')


@app.route('/favicon.ico')
def favicon():
    """Serve favicon (browsers often request /favicon.ico even if link tags exist)."""
    return redirect(url_for('static', filename='logo/favicon_io (3)/favicon.ico'))


@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('static', 'sitemap.xml', mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt', mimetype='text/plain')


@app.route('/api/generate', methods=['POST'])
def api_generate():
    """API tạo QR preview"""
    try:
        # Sanitize and validate all inputs
        qr_type = sanitize_input(request.form.get('qr_type', ''), max_length=30).lower()
        data = sanitize_qr_data(request.form.get('data', 'https://qrio.vn'), max_length=4000)
        if not data:
            data = 'https://qrio.vn'

        # VietQR: generate a bank-compatible payload via official public API.
        if qr_type == 'vietqr':
            account_no = sanitize_input(request.form.get('vietqr_account', ''), max_length=32)
            account_name = sanitize_input(request.form.get('vietqr_name', ''), max_length=80)
            acq_id = sanitize_input(request.form.get('vietqr_bank', ''), max_length=16)
            add_info = sanitize_input(request.form.get('vietqr_memo', ''), max_length=120)

            amount_raw = sanitize_input(request.form.get('vietqr_amount', ''), max_length=20)
            try:
                amount_val = int(float(amount_raw)) if amount_raw else None
            except ValueError:
                amount_val = None

            if not account_no or not acq_id:
                return jsonify({'error': 'Vui lòng nhập Số tài khoản và chọn Ngân hàng'}), 400

            payload = {
                'accountNo': account_no,
                'accountName': account_name,
                'acqId': int(acq_id) if str(acq_id).isdigit() else acq_id,
                'amount': amount_val,
                'addInfo': add_info,
                'format': 'text',
            }
            # Remove empty keys
            payload = {k: v for k, v in payload.items() if v not in (None, '')}

            try:
                r = requests.post('https://api.vietqr.io/v2/generate', json=payload, timeout=12)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                return jsonify({'error': f'VietQR API lỗi: {e}'}), 502

            data_obj = resp.get('data') if isinstance(resp, dict) else None
            # Preferred: use returned EMV text payload to preserve our styling pipeline
            if isinstance(data_obj, dict):
                emv_text = data_obj.get('qrCode') or data_obj.get('qrData') or data_obj.get('qrText')
                if isinstance(emv_text, str) and emv_text.strip():
                    data = sanitize_qr_data(emv_text.strip(), max_length=8000)
                else:
                    # Fallback: base64 image
                    qr_data_url = data_obj.get('qrDataURL') or data_obj.get('qrImage')
                    if isinstance(qr_data_url, str) and 'base64,' in qr_data_url:
                        b64 = qr_data_url.split('base64,', 1)[1]
                        try:
                            img_bytes = base64.b64decode(b64)
                            img = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
                            buf = io.BytesIO()
                            img.save(buf, format='PNG')
                            img_base64 = base64.b64encode(buf.getvalue()).decode()
                            return jsonify({'image': f'data:image/png;base64,{img_base64}'})
                        except Exception:
                            return jsonify({'error': 'Không đọc được QR từ VietQR API'}), 502
            else:
                return jsonify({'error': 'VietQR API trả về dữ liệu không hợp lệ'}), 502
        
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
        
        # Track analytics
        track_event('/api/generate', 'generate_qr', get_country_from_request(), get_device_type())
        
        return jsonify(payload)
    except Exception as e:
        print(f"Error in api_generate: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    """API tải QR dưới dạng file (có thể kèm tiêu đề)"""
    try:
        qr_type = sanitize_input(request.form.get('qr_type', ''), max_length=30).lower()
        data = sanitize_qr_data(request.form.get('data', 'https://qrio.vn'), max_length=4000)
        qr_color = validate_hex_color(request.form.get('qr_color', '#0c6c3b'))
        bg_color = validate_hex_color(request.form.get('bg_color', '#ffffff'))
        box_size = validate_int(request.form.get('box_size', 10), default=10, min_val=1, max_val=50)
        dot_type = sanitize_input(request.form.get('dot_type', 'rounded'), max_length=50)
        border = validate_int(request.form.get('border', 4), default=4, min_val=0, max_val=20)
        ecc_level = sanitize_input(request.form.get('ecc_level', 'H'), max_length=1).upper()
        version = _parse_version(request.form.get('version'))
        module_style = sanitize_input(request.form.get('module_style', 'legacy'), max_length=50)
        eye_style = sanitize_input(request.form.get('eye_style', 'square'), max_length=50)
        filename = sanitize_input(request.form.get('filename', 'qr_code'), max_length=80) or 'qr_code'

        if qr_type == 'vietqr':
            account_no = sanitize_input(request.form.get('vietqr_account', ''), max_length=32)
            account_name = sanitize_input(request.form.get('vietqr_name', ''), max_length=80)
            acq_id = sanitize_input(request.form.get('vietqr_bank', ''), max_length=16)
            add_info = sanitize_input(request.form.get('vietqr_memo', ''), max_length=120)

            amount_raw = sanitize_input(request.form.get('vietqr_amount', ''), max_length=20)
            try:
                amount_val = int(float(amount_raw)) if amount_raw else None
            except ValueError:
                amount_val = None

            if not account_no or not acq_id:
                return jsonify({'error': 'Vui lòng nhập Số tài khoản và chọn Ngân hàng'}), 400

            payload = {
                'accountNo': account_no,
                'accountName': account_name,
                'acqId': int(acq_id) if str(acq_id).isdigit() else acq_id,
                'amount': amount_val,
                'addInfo': add_info,
                'format': 'text',
            }
            payload = {k: v for k, v in payload.items() if v not in (None, '')}

            try:
                r = requests.post('https://api.vietqr.io/v2/generate', json=payload, timeout=12)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                return jsonify({'error': f'VietQR API lỗi: {e}'}), 502

            data_obj = resp.get('data') if isinstance(resp, dict) else None
            if isinstance(data_obj, dict):
                emv_text = data_obj.get('qrCode') or data_obj.get('qrData') or data_obj.get('qrText')
                if isinstance(emv_text, str) and emv_text.strip():
                    data = sanitize_qr_data(emv_text.strip(), max_length=8000)
                else:
                    return jsonify({'error': 'VietQR API không trả về dữ liệu QR text'}), 502
            else:
                return jsonify({'error': 'VietQR API trả về dữ liệu không hợp lệ'}), 502

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
        
        # Track analytics (before sending file)
        track_event('/api/download', 'download_qr', get_country_from_request(), get_device_type())
        
        return send_file(buf, mimetype='image/png', as_attachment=True, download_name=f'{filename}.png')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/file/upload', methods=['POST'])
def api_file_upload():
    """Upload a file and return a download URL (used by File QR).

    This endpoint uses a free third-party intermediary by default so that:
    - The QR can point to a direct download link even if this app isn't running.
    - The server does NOT persist the uploaded file in the project folder.

    Note: The uploaded file becomes accessible via a public URL on the chosen service.
    """
    try:
        if 'file' not in request.files or not request.files['file'].filename:
            return jsonify({'error': 'Thiếu file upload'}), 400

        up = request.files['file']
        original_name = up.filename or ''
        safe_name = secure_filename(original_name) or 'download'

        # Prefer the user's original filename for the download name, but sanitize
        # to avoid control characters and header injection.
        download_name = sanitize_input(original_name, max_length=200)
        download_name = download_name.replace('\r', '').replace('\n', '').strip()
        if not download_name:
            download_name = safe_name

        # Enforce 5MB limit (match UI)
        data = up.read()
        if len(data) > 5 * 1024 * 1024:
            return jsonify({'error': 'File quá lớn! Tối đa 5MB'}), 400

        # Proxy-upload to a free public file host so the QR points to a direct
        # download link even if this app isn't running.
        #
        # UX requirement: scan QR -> open link -> download immediately for ANY
        # file type (not a preview page). This generally requires the host to
        # respond with Content-Disposition: attachment.
        #
        # bashupload.com matches this requirement in testing (PDF/PNG/ZIP/EXE),
        # so we use it first.

        def _is_http_url(u: str) -> bool:
            return isinstance(u, str) and (u.startswith('http://') or u.startswith('https://'))

        def _upload_bashupload() -> str:
            # Returns plain text where the first non-empty line is the URL.
            r = requests.post(
                'https://bashupload.com/',
                files={'file': (safe_name, data)},
                headers={'User-Agent': 'qr-editor/1.0', 'Accept': 'text/plain'},
                timeout=60,
            )
            r.raise_for_status()
            text = (r.text or '').strip()
            for line in text.splitlines():
                line = (line or '').strip()
                if _is_http_url(line):
                    return line
            return text

        def _upload_catbox() -> str:
            # Anonymous uploads supported: omit userhash.
            r = requests.post(
                'https://catbox.moe/user/api.php',
                data={'reqtype': 'fileupload'},
                files={'fileToUpload': (safe_name, data)},
                headers={'User-Agent': 'qr-editor/1.0', 'Accept': 'text/plain'},
                timeout=25,
            )
            r.raise_for_status()
            return (r.text or '').strip()

        def _upload_0x0() -> str:
            r = requests.post(
                'https://0x0.st',
                files={'file': (safe_name, data)},
                headers={'User-Agent': 'qr-editor/1.0', 'Accept': 'text/plain'},
                timeout=25,
            )
            r.raise_for_status()
            return (r.text or '').strip()

        def _upload_litterbox() -> str:
            # Temporary link (max 72h) but works anonymously.
            r = requests.post(
                'https://litterbox.catbox.moe/resources/internals/api.php',
                data={'reqtype': 'fileupload', 'time': '72h'},
                files={'fileToUpload': (safe_name, data)},
                headers={'User-Agent': 'qr-editor/1.0', 'Accept': 'text/plain'},
                timeout=25,
            )
            r.raise_for_status()
            return (r.text or '').strip()

        providers = [
            ('bashupload.com (attachment)', _upload_bashupload),
            # Fallbacks below may not force download for all file types, but
            # help keep uploads working if bashupload is down.
            ('catbox.moe', _upload_catbox),
            ('0x0.st', _upload_0x0),
            ('litterbox.catbox.moe (72h)', _upload_litterbox),
        ]

        url = None
        errors = []
        for name, fn in providers:
            try:
                candidate = fn()
                if _is_http_url(candidate):
                    url = candidate
                    break
                errors.append(f"{name}: response không hợp lệ: {repr(candidate)[:200]}")
            except requests.HTTPError as e:
                resp = getattr(e, 'response', None)
                status = getattr(resp, 'status_code', None)
                body = (getattr(resp, 'text', '') or '')
                body = body.strip().replace('\n', ' ')[:200]
                errors.append(f"{name}: HTTP {status} {body}".strip())
            except Exception as e:
                errors.append(f"{name}: {e}")

        if not _is_http_url(url):
            return jsonify({'error': 'Upload trung gian lỗi: ' + ' | '.join(errors)}), 502

        # Optional: return a proxy URL which forces download + preserves filename.
        # This avoids relying on the third-party host's filename/headers.
        proxy_base = (os.environ.get('FILE_QR_DOWNLOAD_PROXY') or '').strip()
        if proxy_base:
            try:
                from urllib.parse import quote

                sep = '&' if '?' in proxy_base else '?'
                proxied = f"{proxy_base}{sep}u={quote(url, safe='')}&name={quote(download_name, safe='')}"
                return jsonify({'url': proxied, 'directUrl': url})
            except Exception:
                # Fall back to direct URL if proxy formatting fails.
                pass

        return jsonify({'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/file/<token>')
def download_uploaded_file(token: str):
    """Download previously uploaded file (Content-Disposition: attachment)."""
    token = sanitize_input(token, max_length=64)
    uploads_dir = Path(__file__).parent / 'data' / 'uploads'
    file_path = uploads_dir / token
    meta_path = uploads_dir / f'{token}.json'

    if not file_path.exists() or not meta_path.exists():
        return jsonify({'error': 'File không tồn tại'}), 404

    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except Exception:
        meta = {'filename': 'download', 'mime': 'application/octet-stream'}

    return send_file(
        str(file_path),
        mimetype=meta.get('mime') or 'application/octet-stream',
        as_attachment=True,
        download_name=meta.get('filename') or 'download',
        max_age=0,
    )


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
