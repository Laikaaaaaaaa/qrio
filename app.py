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


# Tạo thư mục lưu nếu chưa có
thu_muc_ma = Path("mã")
thu_muc_ma.mkdir(exist_ok=True)

# Uploads for "File QR"
uploads_dir = Path('uploads')
uploads_dir.mkdir(exist_ok=True)

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
        data = sanitize_qr_data(request.form.get('data', 'https://qrio.vn'), max_length=4000)
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


@app.route('/api/file/upload', methods=['POST'])
def api_file_upload():
    """Upload a file and return a download URL (used by File QR)."""
    try:
        if 'file' not in request.files or not request.files['file'].filename:
            return jsonify({'error': 'Thiếu file upload'}), 400

        up = request.files['file']
        original_name = up.filename
        safe_name = secure_filename(original_name) or 'download'

        # Enforce 5MB limit (match UI)
        data = up.read()
        if len(data) > 5 * 1024 * 1024:
            return jsonify({'error': 'File quá lớn! Tối đa 5MB'}), 400

        token = uuid.uuid4().hex
        file_path = uploads_dir / token
        meta_path = uploads_dir / f'{token}.json'

        file_path.write_bytes(data)
        mime, _ = mimetypes.guess_type(safe_name)
        meta_path.write_text(
            json.dumps({'filename': safe_name, 'mime': mime or 'application/octet-stream'}, ensure_ascii=False),
            encoding='utf-8',
        )

        url = url_for('download_uploaded_file', token=token, _external=True)
        return jsonify({'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/file/<token>')
def download_uploaded_file(token: str):
    """Download previously uploaded file (Content-Disposition: attachment)."""
    token = sanitize_input(token, max_length=64)
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
