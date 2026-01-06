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
from urllib.parse import urlparse
import logging
import sys

import hashlib
import hmac

import re
import sqlite3

try:
    from flask_socketio import SocketIO, join_room
except Exception:  # pragma: no cover
    SocketIO = None
    join_room = None

# ========================
# PRODUCTION LOGGING SETUP
# ========================
def setup_logging():
    """Configure structured logging for production."""
    log_level = logging.DEBUG if os.environ.get('FLASK_ENV') == 'development' else logging.INFO
    log_format = '%(asctime)s | %(levelname)s | %(name)s | %(message)s'
    
    # Root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    # Suppress noisy libraries in production
    if os.environ.get('FLASK_ENV') != 'development':
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    return logging.getLogger('qrio')

logger = setup_logging()


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

    # Frontend uses these slugs in edit.html.
    if style in (
        'rounded-square',
        'rounded_square',
        'roundedsquare',
    ):
        return RoundedModuleDrawer()

    # Rounded bars / lines
    if style in (
        'rounded-bar',
        'rounded_bar',
        'roundedbar',
    ):
        return HorizontalBarsDrawer(vertical_shrink=0.9)

    if style in (
        'horizontal-bar',
        'horizontal_bar',
        'horizontalbar',
    ):
        return HorizontalBarsDrawer(vertical_shrink=0.65)

    if style in (
        'vertical-bar',
        'vertical_bar',
        'verticalbar',
    ):
        return VerticalBarsDrawer(horizontal_shrink=0.65)

    # Capsule / pill look (full-height bars)
    if style in (
        'capsule',
        'pill',
        'vien-thuoc',
        'vien_thuoc',
    ):
        return HorizontalBarsDrawer(vertical_shrink=1.0)

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


socketio = None
if SocketIO is not None:
    # Allow overriding async mode (e.g., "eventlet" in production, "threading" on Windows/dev)
    _async_mode = os.environ.get('SOCKETIO_ASYNC_MODE')
    socketio = SocketIO(
        app,
        cors_allowed_origins='*',
        async_mode=_async_mode or None,
        ping_timeout=20,
        ping_interval=25,
    )


def _ws_emit_ticket_updated(ticket_id: str, extra: Optional[dict] = None) -> None:
    if not socketio:
        return
    tid = sanitize_input(ticket_id or '', max_length=80).upper()
    if not tid:
        return
    payload = {'ticketId': tid}
    if isinstance(extra, dict):
        payload.update(extra)
    # Always emit to the ticket-specific room.
    socketio.emit('ticket_updated', payload, room=f'ticket:{tid}')

    # Also fan out to order/event rooms if identifiers are present.
    oid = sanitize_input(payload.get('orderId', ''), max_length=80)
    if oid:
        socketio.emit('ticket_updated', payload, room=f'order:{oid}')
    eid = sanitize_input(payload.get('eventId', ''), max_length=40)
    if eid:
        socketio.emit('ticket_updated', payload, room=f'event:{eid}')


def _ws_emit_tickets_updated(ticket_ids, extra: Optional[dict] = None) -> None:
    if not socketio:
        return
    if not ticket_ids:
        return
    for tid in ticket_ids:
        _ws_emit_ticket_updated(tid, extra=extra)


def _ws_emit_order_updated(order_id: str, event_id: str = '', status: str = '', ticket_ids=None) -> None:
    if not socketio:
        return
    oid = sanitize_input(order_id or '', max_length=80)
    if not oid:
        return
    payload = {'orderId': oid}
    eid = sanitize_input(event_id or '', max_length=40)
    if eid:
        payload['eventId'] = eid
    st = sanitize_input(status or '', max_length=32).lower()
    if st:
        payload['status'] = st
    if isinstance(ticket_ids, list) and ticket_ids:
        payload['ticketIds'] = [sanitize_input(t, max_length=80).upper() for t in ticket_ids if t]

    socketio.emit('order_updated', payload, room=f'order:{oid}')
    if eid:
        socketio.emit('order_updated', payload, room=f'event:{eid}')


if socketio:
    @socketio.on('join')
    def _ws_join(data):
        try:
            if not isinstance(data, dict):
                data = {}
            ticket_id = sanitize_input((data.get('ticketId') or ''), max_length=80).upper()
            order_id = sanitize_input((data.get('orderId') or ''), max_length=80)
            event_id = sanitize_input((data.get('eventId') or ''), max_length=40)

            if join_room:
                if ticket_id:
                    join_room(f'ticket:{ticket_id}')
                if order_id:
                    join_room(f'order:{order_id}')
                if event_id:
                    join_room(f'event:{event_id}')
        except Exception:
            # Keep silent: client can still function without rooms.
            pass


# ========================
# TICKET SYSTEM (SePay)
# ========================

TICKETS_DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'tickets.db')

_TICKETS_DB_INITIALIZED = False


def _hash_owner_password(event_id: str, password: str) -> str:
    """Hash owner password (scoped to event_id).

    This is not meant to be a full auth system; it is a lightweight gate
    to prevent casual access to the manage-orders page.
    """
    event_id = sanitize_input(event_id or '', max_length=80)
    password = sanitize_input(password or '', max_length=400)
    blob = (event_id + ':' + password).encode('utf-8', errors='ignore')
    return hashlib.sha256(blob).hexdigest()


def _ensure_data_dir(path: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass


def init_tickets_db():
    _ensure_data_dir(TICKETS_DB_PATH)
    conn = sqlite3.connect(TICKETS_DB_PATH, timeout=5)
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS ticket_events (
                event_id TEXT PRIMARY KEY,
                event_name TEXT NOT NULL,
                description TEXT,
                price_per_ticket INTEGER NOT NULL,
                max_tickets INTEGER NOT NULL,
                bank_code TEXT NOT NULL,
                bank_name TEXT NOT NULL,
                account_number TEXT NOT NULL,
                account_name TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                payment_method TEXT DEFAULT 'bank_api',
                bank_api_key TEXT,
                sepay_api_key TEXT,
                owner_password_hash TEXT,
                created_at REAL NOT NULL
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at)')

        # Add new columns to existing table if missing
        try:
            cur.execute('ALTER TABLE ticket_events ADD COLUMN payment_method TEXT DEFAULT "bank_api"')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_events ADD COLUMN bank_api_key TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_events ADD COLUMN sepay_api_key TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_events ADD COLUMN owner_password_hash TEXT')
        except sqlite3.OperationalError:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS ticket_orders (
                order_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                buyer_name TEXT NOT NULL,
                buyer_email TEXT,
                buyer_phone TEXT,
                buyer_note TEXT,
                quantity INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                payment_type TEXT DEFAULT 'transfer',
                payment_proof_image TEXT,
                cash_payer_name TEXT,
                cash_payment_time TEXT,
                created_at REAL NOT NULL,
                paid_at REAL,
                sepay_transaction_id INTEGER,
                sepay_reference_code TEXT,
                FOREIGN KEY(event_id) REFERENCES ticket_events(event_id)
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_orders_event_id ON ticket_orders(event_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_orders_status ON ticket_orders(status)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_orders_created_at ON ticket_orders(created_at)')

        # Add new columns to existing table if missing
        try:
            cur.execute('ALTER TABLE ticket_orders ADD COLUMN payment_type TEXT DEFAULT "transfer"')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_orders ADD COLUMN payment_proof_image TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_orders ADD COLUMN cash_payer_name TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_orders ADD COLUMN cash_payment_time TEXT')
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute('ALTER TABLE ticket_orders ADD COLUMN buyer_note TEXT')
        except sqlite3.OperationalError:
            pass

        # Ticket items (server-side registry for scanning across devices)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS ticket_items (
                ticket_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                ticket_number INTEGER,
                total_tickets INTEGER,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                used_at REAL,
                cancelled_at REAL,
                FOREIGN KEY(order_id) REFERENCES ticket_orders(order_id)
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_items_order_id ON ticket_items(order_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_ticket_items_status ON ticket_items(status)')

        # Add new columns to existing table if missing
        try:
            cur.execute('ALTER TABLE ticket_items ADD COLUMN ticket_number INTEGER')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_items ADD COLUMN total_tickets INTEGER')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_items ADD COLUMN used_at REAL')
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute('ALTER TABLE ticket_items ADD COLUMN cancelled_at REAL')
        except sqlite3.OperationalError:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS sepay_webhook_dedupe (
                sepay_id INTEGER PRIMARY KEY,
                received_at REAL NOT NULL
            )
        ''')
        conn.commit()
    finally:
        conn.close()


def get_tickets_db():
    global _TICKETS_DB_INITIALIZED
    # Always run migrations once per process even if the DB file already exists.
    if not _TICKETS_DB_INITIALIZED:
        init_tickets_db()
        _TICKETS_DB_INITIALIZED = True
    conn = sqlite3.connect(TICKETS_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _new_event_id() -> str:
    # Short but random enough for public URLs.
    return 'EV' + uuid.uuid4().hex[:10].upper()


def _new_order_id() -> str:
    return 'TK' + uuid.uuid4().hex[:10].upper()


def _new_ticket_id() -> str:
    # Public ticket code shown to buyer/staff.
    return 'VE' + uuid.uuid4().hex[:12].upper()


def _get_base_url() -> str:
    # request.host_url ends with '/'
    return request.host_url.rstrip('/')


def _parse_sepay_apikey_header(auth_header: str) -> str:
    if not auth_header:
        return ''
    # Expected format: "Apikey <API_KEY>"
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2:
        return ''
    scheme, token = parts[0].strip(), parts[1].strip()
    if scheme.lower() != 'apikey':
        return ''
    return token

# ========================
# RATE LIMITING (Production)
# ========================
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

def get_real_ip():
    """Get real client IP behind proxies."""
    if os.environ.get('FLASK_ENV') == 'production':
        # Trust proxy headers in production
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        cf_ip = request.headers.get('CF-Connecting-IP', '')
        if cf_ip:
            return cf_ip.strip()
    return get_remote_address()

limiter = Limiter(
    key_func=get_real_ip,
    app=app,
    default_limits=["10 per second", "100 per minute", "1000 per hour", "10000 per day"],
    storage_uri="memory://",
    strategy="fixed-window",
)

# Exempt static files from rate limiting
@limiter.request_filter
def _skip_static():
    return request.path.startswith('/static/')

# ========================
# ADMIN DASHBOARD INTEGRATION
# ========================
from admin import admin_bp, analytics_bp, track_event, store_contact_message

app.register_blueprint(admin_bp)
app.register_blueprint(analytics_bp)


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _build_edit_landing_html(*, canonical_url: str, title: str, description: str) -> str:
    """Return edit.html with landing-specific SEO tags (no UI changes)."""
    edit_path = Path(__file__).resolve().parent / 'edit.html'
    html = _read_text_file(edit_path)

    # Canonical & social URLs
    html = html.replace(
        '<link rel="canonical" href="https://qrio.site/edit.html" />',
        f'<link rel="canonical" href="{canonical_url}" />',
        1,
    )
    html = html.replace(
        '<meta property="og:url" content="https://qrio.site/edit.html" />',
        f'<meta property="og:url" content="{canonical_url}" />',
        1,
    )
    html = html.replace(
        '<meta name="twitter:url" content="https://qrio.site/edit.html" />',
        f'<meta name="twitter:url" content="{canonical_url}" />',
        1,
    )

    # Title (remove data-i18n to prevent later JS from overwriting landing-specific SEO title)
    if '<title data-i18n="meta.title">' in html:
        html = html.replace(
            '<title data-i18n="meta.title">Qrio - Trình chỉnh sửa mã QR miễn phí</title>',
            f'<title>{title}</title>',
            1,
        )

    # Description
    html = html.replace(
        '<meta name="description" content="Thiết kế mã QR chuyên nghiệp với trình chỉnh sửa mạnh mẽ. Tùy chỉnh màu sắc, kiểu dáng, thêm logo và xuất ảnh HD." />',
        f'<meta name="description" content="{description}" />',
        1,
    )

    # Social titles/descriptions
    html = html.replace(
        '<meta property="og:title" content="Qrio - Trình chỉnh sửa mã QR" />',
        f'<meta property="og:title" content="{title}" />',
        1,
    )
    html = html.replace(
        '<meta property="og:description" content="Thiết kế mã QR chuyên nghiệp với trình chỉnh sửa mạnh mẽ." />',
        f'<meta property="og:description" content="{description}" />',
        1,
    )
    html = html.replace(
        '<meta name="twitter:title" content="Qrio Editor" />',
        f'<meta name="twitter:title" content="{title}" />',
        1,
    )
    html = html.replace(
        '<meta name="twitter:description" content="Thiết kế mã QR chuyên nghiệp." />',
        f'<meta name="twitter:description" content="{description}" />',
        1,
    )

    # JSON-LD: replace the first WebPage block we added earlier
    json_ld_old = (
        '<script type="application/ld+json">\n'
        '    {\n'
        '      "@context": "https://schema.org",\n'
        '      "@type": "WebPage",\n'
        '      "name": "Qrio - Trình chỉnh sửa mã QR",\n'
        '      "url": "https://qrio.site/edit.html",\n'
        '      "inLanguage": "vi"\n'
        '    }\n'
        '  </script>'
    )
    json_ld_new = (
        '<script type="application/ld+json">\n'
        '    {\n'
        '      "@context": "https://schema.org",\n'
        '      "@type": "WebPage",\n'
        f'      "name": {json.dumps(title)},\n'
        f'      "url": {json.dumps(canonical_url)},\n'
        '      "inLanguage": "vi"\n'
        '    }\n'
        '  </script>'
    )
    html = html.replace(json_ld_old, json_ld_new, 1)

    return html


# Security headers - CSP, XSS protection, cache
@app.after_request
def add_security_headers(response):
    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://unpkg.com; "
        "font-src 'self' blob: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
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


# ========================
# HEALTH CHECK ENDPOINT
# ========================
@app.route('/healthz')
@limiter.exempt
def healthz():
    """Health check for load balancers and monitoring."""
    checks = {
        'status': 'healthy',
        'timestamp': time.time(),
        'checks': {}
    }
    
    # Check analytics DB
    try:
        from admin import ANALYTICS_DB_PATH
        import sqlite3
        conn = sqlite3.connect(ANALYTICS_DB_PATH, timeout=2)
        conn.execute('SELECT 1')
        conn.close()
        checks['checks']['analytics_db'] = 'ok'
    except Exception as e:
        checks['checks']['analytics_db'] = f'error: {e}'
        checks['status'] = 'degraded'
    
    # Check session DB
    try:
        from admin import SESSION_DB_PATH
        conn = sqlite3.connect(SESSION_DB_PATH, timeout=2)
        conn.execute('SELECT 1')
        conn.close()
        checks['checks']['session_db'] = 'ok'
    except Exception as e:
        checks['checks']['session_db'] = f'error: {e}'
        checks['status'] = 'degraded'
    
    status_code = 200 if checks['status'] == 'healthy' else 503
    return jsonify(checks), status_code


@app.route('/ready')
@limiter.exempt
def ready():
    """Readiness probe - returns 200 when app can serve traffic."""
    return jsonify({'ready': True}), 200


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
        logger.error(f"Lỗi tạo QR: {e}")
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


def get_source_from_request() -> str:
    """Rudimentary traffic source classification (gg, fb, direct, other)."""
    utm = (request.args.get('utm_source', '') or '').strip().lower()
    if utm:
        if utm in ('gg', 'google'):
            return 'gg'
        if utm in ('fb', 'facebook'):
            return 'fb'
        if utm == 'direct':
            return 'direct'
        return 'other'

    ref = (request.headers.get('Referer', '') or '').strip()
    if not ref:
        return 'direct'
    try:
        host = (urlparse(ref).netloc or '').lower()
    except Exception:
        host = ''
    if not host:
        return 'direct'
    if 'google.' in host or host == 'google.com':
        return 'gg'
    if 'facebook.' in host or host == 'fb.com' or host.endswith('.fb.com') or 'l.facebook.com' in host:
        return 'fb'
    return 'other'


@app.route('/')
def index():
    """Serve trang chủ mặc định (home)."""
    track_event('/', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'home.html')


@app.route('/home.html')
def home_html():
    """Alias: truy cập trực tiếp home.html"""
    return send_from_directory('.', 'home.html')


# ========================
# ELECTRONIC TICKET SYSTEM
# ========================

@app.route('/ticket-create')
@app.route('/ticket-create.html')
def ticket_create_html():
    """Page to create electronic tickets."""
    track_event('/ticket-create', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'ticket-create.html')


@app.route('/ticket-create/c/<event_id>')
@app.route('/ticket-create.html/c/<event_id>')
def ticket_create_owner_link(event_id):
    """Owner deep-link that preselects the event and forwards to owner page."""
    event_id = sanitize_input(event_id, max_length=40)
    track_event('/ticket-create/c', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return redirect(f"/owner-ticket.html?event={event_id}", code=302)


@app.route('/buy-ticket')
@app.route('/buy-ticket.html')
def buy_ticket_html():
    """Page for customers to buy tickets."""
    track_event('/buy-ticket', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'buy-ticket.html')


@app.route('/transfer')
@app.route('/transfer.html')
def transfer_html():
    """Page for customers to pay via bank transfer."""
    track_event('/transfer', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'transfer.html')


@app.route('/transfer-confirm')
@app.route('/transfer-confirm.html')
def transfer_confirm_html():
    """Page for customers to confirm/verify a bank transfer."""
    track_event('/transfer-confirm', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'transfer-confirm.html')


@app.route('/ticket')
@app.route('/ticket.html')
def ticket_html():
    """Page to view electronic ticket."""
    track_event('/ticket', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'ticket.html')


@app.route('/scan')
@app.route('/scan.html')
def scan_html():
    """Page for staff to scan ticket QR codes."""
    track_event('/scan', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'scan.html')


@app.route('/manage-orders')
@app.route('/manage-orders.html')
def manage_orders_html():
    """Page for event owners to manage orders."""
    track_event('/manage-orders', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'manage-orders.html')


@app.route('/owner-ticket')
@app.route('/owner-ticket.html')
def owner_ticket_html():
    """Owner landing page for a specific event."""
    track_event('/owner-ticket', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'owner-ticket.html')


@app.route('/order-history')
@app.route('/order-history.html')
def order_history_html():
    """Page for event owners to view order history."""
    track_event('/order-history', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'order-history.html')


@app.route('/information')
@app.route('/information.html')
def information_html():
    """Page showing ticket information after scanning."""
    track_event('/information', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'information.html')


@app.route('/sepay-webhook-guide')
@app.route('/sepay-webhook-guide.html')
def sepay_webhook_guide_html():
    """Guide for shop owners to set up SePay WebHooks."""
    track_event('/sepay-webhook-guide', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'sepay-webhook-guide.html')


@app.route('/edit')
def edit_redirect():
    """Canonicalize old /edit to /edit.html (avoid duplicate indexing)."""
    return redirect('/edit.html', code=301)


@app.route('/edit.html')
def edit_html():
    """Trang chỉnh sửa (tên cũ: index)."""
    track_event('/edit', 'page_view', get_country_from_request(), get_device_type(), source=get_source_from_request())
    return send_from_directory('.', 'edit.html')


@app.route('/generate')
def generate_root():
    """SEO-friendly entrypoint for generator deep links."""
    return redirect('/edit.html', code=302)


@app.route('/generate/<slug>')
def generate_landing(slug: str):
    """Landing pages that map common search intents to specific QR types in the editor.

    Example:
      - /generate/vcard -> vCard tab
      - /generate/appointment -> Event tab
    """
    slug_norm = (slug or '').strip().lower()

    # Map slugs (English URLs) to existing editor types.
    type_map = {
        'vcard': 'vcard',
        'vcf': 'vcard',
        'appointment': 'event',
        'schedule': 'event',
        'calendar': 'event',
        'event': 'event',
    }

    qr_type = type_map.get(slug_norm)
    if not qr_type:
        return redirect('/edit.html', code=302)

    canonical_url = f'https://qrio.site/generate/{slug_norm}'
    if slug_norm == 'vcard':
        title = 'Qrio - Tạo mã QR vCard miễn phí'
        description = 'Tạo mã QR vCard (danh thiếp) miễn phí: họ tên, số điện thoại, email, công ty. Tùy chỉnh màu sắc, thêm logo và tải ảnh HD.'
    else:
        title = 'Qrio - Tạo mã QR đặt lịch (Event) miễn phí'
        description = 'Tạo mã QR đặt lịch/sự kiện miễn phí: tiêu đề, địa điểm, thời gian bắt đầu/kết thúc. Tùy chỉnh đẹp mắt, thêm logo và tải ảnh HD.'

    html = _build_edit_landing_html(canonical_url=canonical_url, title=title, description=description)
    # Ensure correct tab is selected even for direct visits without query params.
    html = html.replace(
        '</head>',
        f'  <script>window.__QRIO_INITIAL_QR_TYPE = {json.dumps(qr_type)};</script>\n</head>',
        1,
    )
    return Response(html, mimetype='text/html')


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


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@app.route('/api/contact', methods=['POST'])
@limiter.limit("5 per minute")
def api_contact():
    """Public API: receive contact form and store for admin review."""
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = request.form.to_dict(flat=True)

        name = sanitize_input(payload.get('name', ''), max_length=120)
        email = sanitize_input(payload.get('email', ''), max_length=160)
        subject = sanitize_input(payload.get('subject', ''), max_length=200)
        message = sanitize_input(payload.get('message', ''), max_length=5000)

        if not message or len(message.strip()) < 3:
            return jsonify({'ok': False, 'error': 'Nội dung quá ngắn.'}), 400
        if not email or not _EMAIL_RE.match(email):
            return jsonify({'ok': False, 'error': 'Email không hợp lệ.'}), 400

        store_contact_message(
            name=name,
            email=email,
            subject=subject,
            message=message,
            page='/contact',
            source=get_source_from_request(),
            device=get_device_type(),
            country=get_country_from_request(),
        )

        return jsonify({'ok': True})
    except Exception:
        return jsonify({'ok': False, 'error': 'Không thể gửi tin nhắn.'}), 500


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
@limiter.limit("10 per second; 100 per minute; 1000 per hour; 10000 per day")
def api_generate():
    """API tạo QR preview"""
    try:
        # Sanitize and validate all inputs
        qr_type = sanitize_input(request.form.get('qr_type', ''), max_length=30).lower()
        data = sanitize_qr_data(request.form.get('data', 'https://qrio.site'), max_length=4000)
        if not data:
            data = 'https://qrio.site'

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
        track_event(
            '/api/generate',
            'generate_qr',
            get_country_from_request(),
            get_device_type(),
            qr_type=qr_type,
            source=get_source_from_request(),
        )
        
        return jsonify(payload)
    except Exception as e:
        logger.error(f"Error in api_generate: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/verify-payment', methods=['POST'])
@limiter.limit("2 per second; 30 per minute")
def api_verify_payment():
    """Verify payment status for an order.

    This checks server-side state that is updated by SePay WebHooks.
    """
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = {}

        order_id = sanitize_input(payload.get('orderId', ''), max_length=80)

        if not order_id:
            return jsonify({'ok': False, 'verified': False, 'error': 'Thiếu orderId'}), 400

        try:
            conn = get_tickets_db()
            cur = conn.cursor()
            cur.execute('SELECT status, total_amount, paid_at FROM ticket_orders WHERE order_id = ?', (order_id,))
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not row:
            return jsonify({'ok': True, 'verified': False, 'reason': 'order_not_found', 'message': 'Không tìm thấy đơn hàng.'}), 200

        status = (row['status'] or '').lower()
        if status == 'paid':
            return jsonify({'ok': True, 'verified': True, 'orderId': order_id}), 200

        return jsonify({
            'ok': True,
            'verified': False,
            'reason': 'not_paid_yet',
            'message': 'Chưa nhận được xác nhận thanh toán từ SePay. Vui lòng đợi 10-60 giây và thử lại.',
        }), 200
    except Exception:
        return jsonify({'ok': False, 'verified': False, 'error': 'Không thể kiểm tra thanh toán. Vui lòng thử lại.'}), 500


@app.route('/api/ticket/events', methods=['POST'])
@limiter.limit("4 per second; 60 per minute")
def api_ticket_create_event():
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = {}

        event_name = sanitize_input(payload.get('eventName', ''), max_length=120)
        description = sanitize_input(payload.get('description', ''), max_length=400)
        price_per_ticket = validate_int(payload.get('pricePerTicket'), default=0, min_val=0, max_val=10**9)
        max_tickets = validate_int(payload.get('maxTickets'), default=10, min_val=1, max_val=10**4)
        bank_code = sanitize_input(payload.get('bankCode', ''), max_length=16)
        bank_name = sanitize_input(payload.get('bankName', ''), max_length=64)
        account_number = re.sub(r'[^0-9]', '', sanitize_input(payload.get('accountNumber', ''), max_length=32))
        account_name = sanitize_input(payload.get('accountName', ''), max_length=80).upper()
        start_date = sanitize_input(payload.get('startDate', ''), max_length=20)
        end_date = sanitize_input(payload.get('endDate', ''), max_length=20)
        payment_method = sanitize_input(payload.get('paymentMethod', 'bank_api'), max_length=32)
        bank_api_key = sanitize_input(payload.get('bankApiKey', ''), max_length=200)
        sepay_api_key = sanitize_input(payload.get('sepayApiKey', ''), max_length=200)
        owner_password = sanitize_input(payload.get('ownerPassword', ''), max_length=200)

        # Validate payment method
        if payment_method not in ('bank_api', 'sepay_webhook', 'manual'):
            payment_method = 'bank_api'

        if not event_name:
            return jsonify({'error': 'Vui lòng nhập tên sự kiện/sản phẩm'}), 400
        if price_per_ticket < 1000:
            return jsonify({'error': 'Giá vé tối thiểu 1,000 VND'}), 400
        if not bank_code or not bank_name:
            return jsonify({'error': 'Vui lòng chọn ngân hàng'}), 400
        if not account_number:
            return jsonify({'error': 'Vui lòng nhập số tài khoản (chỉ gồm chữ số)'}), 400
        if not account_name:
            return jsonify({'error': 'Vui lòng nhập tên chủ tài khoản'}), 400
        
        # Validate API keys based on payment method
        if payment_method == 'bank_api' and not bank_api_key:
            return jsonify({'error': 'Vui lòng nhập API Key ngân hàng'}), 400
        if payment_method == 'sepay_webhook' and not sepay_api_key:
            return jsonify({'error': 'Vui lòng nhập SePay API Key'}), 400

        # Owner password gate for manage-orders
        if not owner_password or len(owner_password) < 4:
            return jsonify({'error': 'Vui lòng nhập mật khẩu quản lý (tối thiểu 4 ký tự)'}), 400

        event_id = _new_event_id()
        now = time.time()
        owner_password_hash = _hash_owner_password(event_id, owner_password)

        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO ticket_events (
                    event_id, event_name, description, price_per_ticket, max_tickets,
                    bank_code, bank_name, account_number, account_name,
                    start_date, end_date, payment_method, bank_api_key, sepay_api_key, owner_password_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                event_id, event_name, description, int(price_per_ticket), int(max_tickets),
                bank_code, bank_name, account_number, account_name,
                start_date or None, end_date or None, payment_method, bank_api_key or None, sepay_api_key or None, owner_password_hash, now
            ))
            conn.commit()
        finally:
            conn.close()

        buy_link = f"{_get_base_url()}/buy-ticket.html?event={event_id}"
        webhook_url = f"{_get_base_url()}/api/sepay/webhook/{event_id}" if payment_method == 'sepay_webhook' else ''
        manage_url = f"{_get_base_url()}/manage-orders.html?event={event_id}"

        return jsonify({
            'eventId': event_id,
            'buyLink': buy_link,
            'webhookUrl': webhook_url,
            'manageUrl': manage_url,
            'paymentMethod': payment_method,
        }), 201
    except Exception as e:
        logger.error(f"Error in api_ticket_create_event: {e}")
        return jsonify({'error': 'Không thể tạo sự kiện. Vui lòng thử lại.'}), 500


@app.route('/api/ticket/events/<event_id>', methods=['GET'])
@limiter.limit("10 per second; 120 per minute")
def api_ticket_get_event(event_id):
    try:
        event_id = sanitize_input(event_id, max_length=40)
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                SELECT event_id, event_name, description, price_per_ticket, max_tickets,
                       bank_code, bank_name, account_number, account_name, start_date, end_date, payment_method
                FROM ticket_events WHERE event_id = ?
            ''', (event_id,))
            row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return jsonify({'error': 'Không tìm thấy sự kiện'}), 404

        return jsonify({
            'eventId': row['event_id'],
            'eventName': row['event_name'],
            'description': row['description'] or '',
            'pricePerTicket': int(row['price_per_ticket']),
            'maxTickets': int(row['max_tickets']),
            'bankCode': row['bank_code'],
            'bankName': row['bank_name'],
            'accountNumber': row['account_number'],
            'accountName': row['account_name'],
            'startDate': row['start_date'] or '',
            'endDate': row['end_date'] or '',
            'paymentMethod': row['payment_method'] or 'bank_api',
        }), 200
    except Exception:
        return jsonify({'error': 'Không thể tải thông tin sự kiện'}), 500


@app.route('/api/ticket/events/<event_id>/owner', methods=['GET'])
@limiter.limit("10 per second; 120 per minute")
def api_ticket_get_event_owner(event_id):
    """Owner-only view for an event (password-gated)."""
    try:
        event_id = sanitize_input(event_id, max_length=40)
        owner_password = sanitize_input(request.headers.get('X-Owner-Password', ''), max_length=200)

        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                SELECT event_id, event_name, description, price_per_ticket, max_tickets,
                       bank_code, bank_name, account_number, account_name,
                       start_date, end_date, payment_method, owner_password_hash
                FROM ticket_events WHERE event_id = ?
            ''', (event_id,))
            row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return jsonify({'error': 'Không tìm thấy sự kiện'}), 404

        stored_hash = (row['owner_password_hash'] or '').strip()
        # Backward compatible: if no password was set, allow access.
        if stored_hash:
            if not owner_password:
                return jsonify({'error': 'Vui lòng nhập mật khẩu chủ vé'}), 401
            provided_hash = _hash_owner_password(event_id, owner_password)
            if not hmac.compare_digest(stored_hash, provided_hash):
                return jsonify({'error': 'Mật khẩu không đúng'}), 401

        buy_link = f"{_get_base_url()}/buy-ticket.html?event={event_id}"
        owner_link = f"{_get_base_url()}/ticket-create.html/c/{event_id}"

        return jsonify({
            'eventId': row['event_id'],
            'eventName': row['event_name'],
            'description': row['description'] or '',
            'pricePerTicket': int(row['price_per_ticket']),
            'maxTickets': int(row['max_tickets']),
            'bankCode': row['bank_code'],
            'bankName': row['bank_name'],
            'accountNumber': row['account_number'],
            'accountName': row['account_name'],
            'startDate': row['start_date'] or '',
            'endDate': row['end_date'] or '',
            'paymentMethod': row['payment_method'] or 'bank_api',
            'buyLink': buy_link,
            'ownerLink': owner_link,
        }), 200
    except Exception:
        return jsonify({'error': 'Không thể tải trang chủ vé'}), 500


@app.route('/api/ticket/orders', methods=['POST'])
@limiter.limit("4 per second; 60 per minute")
def api_ticket_create_order():
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = {}

        event_id = sanitize_input(payload.get('eventId', ''), max_length=40)
        buyer_name = sanitize_input(payload.get('buyerName', ''), max_length=120)
        buyer_email = sanitize_input(payload.get('buyerEmail', ''), max_length=120)
        buyer_phone = sanitize_input(payload.get('buyerPhone', ''), max_length=40)
        buyer_note = sanitize_input(payload.get('buyerNote', ''), max_length=800)
        quantity = validate_int(payload.get('quantity'), default=1, min_val=1, max_val=10**4)
        payment_type = sanitize_input(payload.get('paymentType', 'transfer'), max_length=32)
        cash_payer_name = sanitize_input(payload.get('cashPayerName', ''), max_length=120)
        cash_payment_time = sanitize_input(payload.get('cashPaymentTime', ''), max_length=64)

        # Validate payment type
        if payment_type not in ('transfer', 'cash'):
            payment_type = 'transfer'

        if not event_id:
            return jsonify({'error': 'Thiếu eventId'}), 400
        if not buyer_name:
            return jsonify({'error': 'Vui lòng nhập họ tên'}), 400

        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT price_per_ticket, max_tickets, bank_code, bank_name, account_number, account_name, payment_method FROM ticket_events WHERE event_id = ?', (event_id,))
            event_row = cur.fetchone()
            if not event_row:
                return jsonify({'error': 'Không tìm thấy sự kiện'}), 404

            max_tickets = int(event_row['max_tickets'])
            if quantity < 1 or quantity > max_tickets:
                return jsonify({'error': f'Số lượng vé tối đa là {max_tickets}'}), 400

            price_per_ticket = int(event_row['price_per_ticket'])
            total_amount = int(price_per_ticket * quantity)
            order_id = _new_order_id()
            now = time.time()

            cur.execute('''
                INSERT INTO ticket_orders (
                    order_id, event_id, buyer_name, buyer_email, buyer_phone,
                    buyer_note,
                    quantity, total_amount, status, payment_type, cash_payer_name, cash_payment_time, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                order_id, event_id, buyer_name, buyer_email or None, buyer_phone or None,
                buyer_note or None,
                int(quantity), int(total_amount), 'pending', payment_type,
                cash_payer_name or None, cash_payment_time or None, now
            ))

            # Create ticket items on server so staff scanning works across devices.
            ticket_status = 'pending'
            if payment_type == 'cash':
                ticket_status = 'pending_payment'

            ticket_ids = []
            items = []
            for i in range(int(quantity)):
                tid = _new_ticket_id()
                ticket_ids.append(tid)
                items.append((tid, order_id, i + 1, int(quantity), ticket_status, now))

            cur.executemany('''
                INSERT INTO ticket_items (ticket_id, order_id, ticket_number, total_tickets, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', items)
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({
            'orderId': order_id,
            'eventId': event_id,
            'quantity': int(quantity),
            'pricePerTicket': int(price_per_ticket),
            'totalAmount': int(total_amount),
            'ticketIds': ticket_ids,
            'memo': order_id,
            'bankCode': event_row['bank_code'],
            'bankName': event_row['bank_name'],
            'accountNumber': event_row['account_number'],
            'accountName': event_row['account_name'],
            'paymentType': payment_type,
            'buyerNote': buyer_note,
            'paymentMethod': event_row['payment_method'] or 'bank_api',
        }), 201
    except Exception as e:
        logger.error(f"Error in api_ticket_create_order: {e}")
        return jsonify({'error': 'Không thể tạo đơn hàng. Vui lòng thử lại.'}), 500


@app.route('/api/ticket/orders/<order_id>', methods=['GET'])
@limiter.limit("10 per second; 120 per minute")
def api_ticket_get_order(order_id):
    try:
        order_id = sanitize_input(order_id, max_length=80)
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                SELECT o.order_id, o.event_id, o.buyer_name, o.buyer_email, o.buyer_phone,
                       o.status, o.total_amount, o.quantity, o.created_at, o.paid_at,
                       o.payment_type, o.payment_proof_image, o.cash_payer_name, o.cash_payment_time,
                       o.buyer_note,
                       e.event_name, e.payment_method
                FROM ticket_orders o
                JOIN ticket_events e ON o.event_id = e.event_id
                WHERE o.order_id = ?
            ''', (order_id,))
            row = cur.fetchone()

            ticket_ids = []
            if row:
                cur.execute('SELECT ticket_id FROM ticket_items WHERE order_id = ? ORDER BY COALESCE(ticket_number, 0) ASC, ticket_id ASC', (order_id,))
                ticket_ids = [r['ticket_id'] for r in cur.fetchall()]
        finally:
            conn.close()

        if not row:
            return jsonify({'error': 'Không tìm thấy đơn hàng'}), 404

        return jsonify({
            'orderId': row['order_id'],
            'eventId': row['event_id'],
            'eventName': row['event_name'],
            'buyerName': row['buyer_name'],
            'buyerEmail': row['buyer_email'],
            'buyerPhone': row['buyer_phone'],
            'buyerNote': row['buyer_note'] or '',
            'status': row['status'],
            'totalAmount': int(row['total_amount']),
            'quantity': int(row['quantity']),
            'ticketIds': ticket_ids,
            'createdAt': row['created_at'],
            'paidAt': row['paid_at'],
            'paymentType': row['payment_type'] or 'transfer',
            'paymentProofImage': row['payment_proof_image'],
            'cashPayerName': row['cash_payer_name'],
            'cashPaymentTime': row['cash_payment_time'],
            'paymentMethod': row['payment_method'] or 'bank_api',
        }), 200
    except Exception:
        return jsonify({'error': 'Không thể tải đơn hàng'}), 500


@app.route('/api/ticket/events/<event_id>/orders', methods=['GET'])
@limiter.limit("10 per second; 120 per minute")
def api_ticket_list_orders(event_id):
    """List all orders for an event (for event owner to manage)."""
    try:
        event_id = sanitize_input(event_id, max_length=40)
        owner_password = sanitize_input(request.headers.get('X-Owner-Password', ''), max_length=200)
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            # Check if event exists
            cur.execute('SELECT event_id, event_name, payment_method, owner_password_hash FROM ticket_events WHERE event_id = ?', (event_id,))
            event_row = cur.fetchone()
            if not event_row:
                return jsonify({'error': 'Không tìm thấy sự kiện'}), 404

            stored_hash = (event_row['owner_password_hash'] or '').strip()
            # Backward compatible: if no password was set, allow access.
            if stored_hash:
                if not owner_password:
                    return jsonify({'error': 'Vui lòng nhập mật khẩu quản lý'}), 401
                provided_hash = _hash_owner_password(event_id, owner_password)
                if not hmac.compare_digest(stored_hash, provided_hash):
                    return jsonify({'error': 'Mật khẩu không đúng'}), 401

            cur.execute('''
                SELECT order_id, buyer_name, buyer_email, buyer_phone, quantity, total_amount, 
                       status, payment_type, payment_proof_image, cash_payer_name, cash_payment_time, buyer_note,
                       created_at, paid_at
                FROM ticket_orders WHERE event_id = ?
                ORDER BY created_at DESC
            ''', (event_id,))
            rows = cur.fetchall()
        finally:
            conn.close()

        orders = []
        for row in rows:
            orders.append({
                'orderId': row['order_id'],
                'buyerName': row['buyer_name'],
                'buyerEmail': row['buyer_email'],
                'buyerPhone': row['buyer_phone'],
                'buyerNote': row['buyer_note'] or '',
                'quantity': int(row['quantity']),
                'totalAmount': int(row['total_amount']),
                'status': row['status'],
                'paymentType': row['payment_type'] or 'transfer',
                'paymentProofImage': row['payment_proof_image'],
                'cashPayerName': row['cash_payer_name'],
                'cashPaymentTime': row['cash_payment_time'],
                'createdAt': row['created_at'],
                'paidAt': row['paid_at'],
            })

        return jsonify({
            'eventId': event_row['event_id'],
            'eventName': event_row['event_name'],
            'paymentMethod': event_row['payment_method'] or 'bank_api',
            'orders': orders,
        }), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_list_orders: {e}")
        return jsonify({'error': 'Không thể tải danh sách đơn hàng'}), 500


@app.route('/api/ticket/tickets/register', methods=['POST'])
@limiter.limit("10 per second; 120 per minute")
def api_ticket_register_tickets():
    """Register ticket IDs on the server so staff scanning works across devices.

    Body can be:
      {"tickets": [{"ticketId": "VE...", "orderId": "TK...", "ticketNumber": 1, "totalTickets": 3, "status": "valid"}, ...]}
    or a single ticket object.
    """
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = {}

        items = payload.get('tickets')
        if isinstance(items, list):
            tickets = items
        else:
            # Allow single-ticket payload
            tickets = [payload]

        if not tickets or not isinstance(tickets, list):
            return jsonify({'error': 'Thiếu tickets'}), 400

        if len(tickets) > 500:
            return jsonify({'error': 'Quá nhiều vé trong một lần đăng ký'}), 400

        now = time.time()
        to_upsert = []

        conn = get_tickets_db()
        try:
            cur = conn.cursor()

            for t in tickets:
                if not isinstance(t, dict):
                    continue

                ticket_id = sanitize_input(t.get('ticketId', ''), max_length=80).upper()
                order_id = sanitize_input(t.get('orderId', ''), max_length=80).upper()
                ticket_number = validate_int(t.get('ticketNumber'), default=0, min_val=0, max_val=10**6)
                total_tickets = validate_int(t.get('totalTickets'), default=0, min_val=0, max_val=10**6)
                status = sanitize_input(t.get('status', 'valid'), max_length=32).lower()

                if not ticket_id or not order_id:
                    continue

                if status not in ('valid', 'paid', 'pending', 'pending_verification', 'pending_payment', 'used', 'cancelled'):
                    status = 'valid'
                if status == 'paid':
                    status = 'valid'

                # Ensure order exists
                cur.execute('SELECT order_id FROM ticket_orders WHERE order_id = ?', (order_id,))
                if not cur.fetchone():
                    continue

                to_upsert.append((ticket_id, order_id, int(ticket_number or 0), int(total_tickets or 0), status, now))

            if not to_upsert:
                return jsonify({'ok': True, 'registered': 0}), 200

            cur.executemany('''
                INSERT INTO ticket_items (ticket_id, order_id, ticket_number, total_tickets, status, created_at)
                VALUES (?, ?, NULLIF(?, 0), NULLIF(?, 0), ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    order_id = excluded.order_id,
                    ticket_number = COALESCE(excluded.ticket_number, ticket_items.ticket_number),
                    total_tickets = COALESCE(excluded.total_tickets, ticket_items.total_tickets),
                    status = CASE
                        WHEN ticket_items.status IN ('used', 'cancelled') THEN ticket_items.status
                        ELSE excluded.status
                    END
            ''', to_upsert)
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({'ok': True, 'registered': len(to_upsert)}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_register_tickets: {e}")
        return jsonify({'error': 'Không thể đăng ký vé. Vui lòng thử lại.'}), 500


@app.route('/api/ticket/tickets/<ticket_id>', methods=['GET'])
@limiter.limit("12 per second; 240 per minute")
def api_ticket_get_ticket(ticket_id):
    """Fetch a ticket by ticket_id for staff scanning."""
    try:
        ticket_id = sanitize_input(ticket_id, max_length=80).upper()
        if not ticket_id:
            return jsonify({'error': 'Thiếu ticketId'}), 400

        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                SELECT
                    t.ticket_id, t.order_id, t.ticket_number, t.total_tickets,
                    t.status AS ticket_status, t.used_at, t.cancelled_at,
                    o.status AS order_status, o.payment_type, o.cash_payer_name, o.cash_payment_time,
                    o.buyer_name, o.buyer_email, o.buyer_phone, o.buyer_note,
                    o.total_amount, o.quantity, o.created_at, o.paid_at,
                    e.event_id, e.event_name, e.description, e.price_per_ticket
                FROM ticket_items t
                JOIN ticket_orders o ON o.order_id = t.order_id
                JOIN ticket_events e ON e.event_id = o.event_id
                WHERE t.ticket_id = ?
            ''', (ticket_id,))
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not row:
            return jsonify({'error': 'Không tìm thấy vé'}), 404

        return jsonify({
            'ticketId': row['ticket_id'],
            'orderId': row['order_id'],
            'ticketNumber': int(row['ticket_number'] or 0) if row['ticket_number'] is not None else None,
            'totalTickets': int(row['total_tickets'] or 0) if row['total_tickets'] is not None else None,
            'ticketStatus': (row['ticket_status'] or 'valid'),
            'usedAt': row['used_at'],
            'cancelledAt': row['cancelled_at'],
            'orderStatus': row['order_status'],
            'paymentType': row['payment_type'] or 'transfer',
            'cashPayerName': row['cash_payer_name'],
            'cashPaymentTime': row['cash_payment_time'],
            'buyerName': row['buyer_name'],
            'buyerEmail': row['buyer_email'],
            'buyerPhone': row['buyer_phone'],
            'buyerNote': row['buyer_note'] or '',
            'totalAmount': int(row['total_amount'] or 0),
            'quantity': int(row['quantity'] or 0),
            'createdAt': row['created_at'],
            'paidAt': row['paid_at'],
            'eventId': row['event_id'],
            'eventName': row['event_name'],
            'description': row['description'] or '',
            'pricePerTicket': int(row['price_per_ticket'] or 0),
        }), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_get_ticket: {e}")
        return jsonify({'error': 'Không thể tải vé'}), 500


@app.route('/api/ticket/tickets/<ticket_id>/use', methods=['POST'])
@limiter.limit("8 per second; 120 per minute")
def api_ticket_mark_used(ticket_id):
    """Mark a ticket as used (server-side), to avoid re-use across devices."""
    try:
        ticket_id = sanitize_input(ticket_id, max_length=80).upper()
        if not ticket_id:
            return jsonify({'error': 'Thiếu ticketId'}), 400

        order_id = ''
        event_id = ''
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT status, order_id FROM ticket_items WHERE ticket_id = ?', (ticket_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Không tìm thấy vé'}), 404

            order_id = (row['order_id'] or '').strip()
            if order_id:
                cur.execute('SELECT event_id FROM ticket_orders WHERE order_id = ?', (order_id,))
                r2 = cur.fetchone()
                if r2:
                    event_id = (r2['event_id'] or '').strip()

            status = (row['status'] or '').lower()
            if status == 'used':
                return jsonify({'ok': True, 'status': 'used'}), 200
            if status == 'cancelled':
                return jsonify({'error': 'Vé đã bị hủy'}), 400

            cur.execute('UPDATE ticket_items SET status = ?, used_at = ? WHERE ticket_id = ?', ('used', time.time(), ticket_id))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        _ws_emit_ticket_updated(ticket_id, extra={'status': 'used', 'orderId': order_id, 'eventId': event_id})
        return jsonify({'ok': True, 'status': 'used'}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_mark_used: {e}")
        return jsonify({'error': 'Không thể cập nhật trạng thái vé'}), 500


@app.route('/api/ticket/tickets/<ticket_id>/cancel', methods=['POST'])
@limiter.limit("8 per second; 120 per minute")
def api_ticket_cancel_ticket(ticket_id):
    """Mark a ticket as cancelled (server-side)."""
    try:
        ticket_id = sanitize_input(ticket_id, max_length=80).upper()
        if not ticket_id:
            return jsonify({'error': 'Thiếu ticketId'}), 400

        order_id = ''
        event_id = ''
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT status, order_id FROM ticket_items WHERE ticket_id = ?', (ticket_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Không tìm thấy vé'}), 404

            order_id = (row['order_id'] or '').strip()
            if order_id:
                cur.execute('SELECT event_id FROM ticket_orders WHERE order_id = ?', (order_id,))
                r2 = cur.fetchone()
                if r2:
                    event_id = (r2['event_id'] or '').strip()

            status = (row['status'] or '').lower()
            if status == 'cancelled':
                return jsonify({'ok': True, 'status': 'cancelled'}), 200
            if status == 'used':
                return jsonify({'error': 'Vé đã sử dụng, không thể hủy'}), 400

            cur.execute('UPDATE ticket_items SET status = ?, cancelled_at = ? WHERE ticket_id = ?', ('cancelled', time.time(), ticket_id))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        _ws_emit_ticket_updated(ticket_id, extra={'status': 'cancelled', 'orderId': order_id, 'eventId': event_id})
        return jsonify({'ok': True, 'status': 'cancelled'}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_cancel_ticket: {e}")
        return jsonify({'error': 'Không thể hủy vé'}), 500


@app.route('/api/ticket/orders/<order_id>/confirm', methods=['POST'])
@limiter.limit("4 per second; 60 per minute")
def api_ticket_confirm_order(order_id):
    """Manually confirm an order as paid (for manual verification)."""
    try:
        order_id = sanitize_input(order_id, max_length=80)
        ticket_ids = []
        event_id = ''
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT status, event_id FROM ticket_orders WHERE order_id = ?', (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Không tìm thấy đơn hàng'}), 404

            event_id = (row['event_id'] or '').strip()

            if row['status'] == 'paid':
                return jsonify({'ok': True, 'message': 'Đơn hàng đã được xác nhận trước đó'}), 200

            now = time.time()
            cur.execute('''
                UPDATE ticket_orders SET status = 'paid', paid_at = ? WHERE order_id = ?
            ''', (now, order_id))

            # Update tickets to valid (do not override used/cancelled)
            cur.execute('''
                UPDATE ticket_items
                SET status = 'valid'
                WHERE order_id = ? AND status NOT IN ('used', 'cancelled')
            ''', (order_id,))

            cur.execute('SELECT ticket_id FROM ticket_items WHERE order_id = ? ORDER BY ticket_id ASC', (order_id,))
            ticket_ids = [r['ticket_id'] for r in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()

        _ws_emit_tickets_updated(ticket_ids, extra={'orderId': order_id, 'orderStatus': 'paid'})
        _ws_emit_order_updated(order_id=order_id, event_id=event_id, status='paid', ticket_ids=ticket_ids)
        return jsonify({'ok': True, 'message': 'Đã xác nhận thanh toán thành công'}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_confirm_order: {e}")
        return jsonify({'error': 'Không thể xác nhận đơn hàng'}), 500


@app.route('/api/ticket/orders/<order_id>/cancel', methods=['POST'])
@limiter.limit("4 per second; 60 per minute")
def api_ticket_cancel_order(order_id):
    """Cancel an order."""
    try:
        order_id = sanitize_input(order_id, max_length=80)
        ticket_ids = []
        event_id = ''
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT status, event_id FROM ticket_orders WHERE order_id = ?', (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Không tìm thấy đơn hàng'}), 404

            event_id = (row['event_id'] or '').strip()

            cur.execute('''
                UPDATE ticket_orders SET status = 'cancelled' WHERE order_id = ?
            ''', (order_id,))

            # Cancel tickets (do not override used)
            cur.execute('''
                UPDATE ticket_items
                SET status = 'cancelled', cancelled_at = COALESCE(cancelled_at, ?)
                WHERE order_id = ? AND status != 'used'
            ''', (time.time(), order_id))

            cur.execute('SELECT ticket_id FROM ticket_items WHERE order_id = ? ORDER BY ticket_id ASC', (order_id,))
            ticket_ids = [r['ticket_id'] for r in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()

        _ws_emit_tickets_updated(ticket_ids, extra={'orderId': order_id, 'orderStatus': 'cancelled'})
        _ws_emit_order_updated(order_id=order_id, event_id=event_id, status='cancelled', ticket_ids=ticket_ids)
        return jsonify({'ok': True, 'message': 'Đã hủy đơn hàng'}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_cancel_order: {e}")
        return jsonify({'error': 'Không thể hủy đơn hàng'}), 500


@app.route('/api/ticket/orders/<order_id>/upload-proof', methods=['POST'])
@limiter.limit("4 per second; 30 per minute")
def api_ticket_upload_proof(order_id):
    """Upload payment proof image for manual verification."""
    try:
        order_id = sanitize_input(order_id, max_length=80)
        
        # Get image from request
        if 'image' not in request.files and 'image' not in (request.form or {}):
            # Try JSON body with base64
            payload = request.get_json(silent=True) if request.is_json else None
            if payload and payload.get('image'):
                image_data = payload.get('image', '')
            else:
                return jsonify({'error': 'Vui lòng tải lên ảnh xác nhận'}), 400
        else:
            if 'image' in request.files:
                file = request.files['image']
                if file.filename == '':
                    return jsonify({'error': 'Vui lòng chọn file ảnh'}), 400
                # Read and encode to base64
                image_bytes = file.read()
                if len(image_bytes) > 5 * 1024 * 1024:  # 5MB limit
                    return jsonify({'error': 'File ảnh quá lớn (tối đa 5MB)'}), 400
                image_data = f"data:{file.content_type};base64,{base64.b64encode(image_bytes).decode()}"
            else:
                image_data = request.form.get('image', '')

        if not image_data:
            return jsonify({'error': 'Vui lòng tải lên ảnh xác nhận'}), 400

        ticket_ids = []
        event_id = ''
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT order_id, status, event_id FROM ticket_orders WHERE order_id = ?', (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Không tìm thấy đơn hàng'}), 404

            event_id = (row['event_id'] or '').strip()

            status = (row['status'] or '').lower()
            new_status = status
            if status not in ('paid', 'cancelled'):
                new_status = 'pending_verification'

            cur.execute('''
                UPDATE ticket_orders
                SET payment_proof_image = ?, status = ?
                WHERE order_id = ?
            ''', (image_data, new_status, order_id))

            if new_status == 'pending_verification':
                cur.execute('''
                    UPDATE ticket_items
                    SET status = 'pending_verification'
                    WHERE order_id = ? AND status NOT IN ('used', 'cancelled')
                ''', (order_id,))

            cur.execute('SELECT ticket_id FROM ticket_items WHERE order_id = ? ORDER BY ticket_id ASC', (order_id,))
            ticket_ids = [r['ticket_id'] for r in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()

        _ws_emit_tickets_updated(ticket_ids, extra={'orderId': order_id, 'orderStatus': new_status})
        _ws_emit_order_updated(order_id=order_id, event_id=event_id, status=new_status, ticket_ids=ticket_ids)
        return jsonify({'ok': True, 'message': 'Đã tải lên ảnh xác nhận'}), 200
    except Exception as e:
        logger.error(f"Error in api_ticket_upload_proof: {e}")
        return jsonify({'error': 'Không thể tải lên ảnh'}), 500


@app.route('/api/sepay/webhook/<event_id>', methods=['POST'])
@limiter.exempt
def api_sepay_webhook(event_id):
    """Receive SePay WebHook transaction notification.

    SePay docs: Authorization header is "Apikey <API_KEY>" (API Key auth).
    We validate against the event's configured sepay_api_key.
    """
    try:
        event_id = sanitize_input(event_id, max_length=40)
        auth_key = _parse_sepay_apikey_header(request.headers.get('Authorization', ''))

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            # Some setups may use form-encoded; try to parse basic fields.
            payload = {}

        # Fetch expected key
        conn = get_tickets_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT sepay_api_key FROM ticket_events WHERE event_id = ?', (event_id,))
            ev = cur.fetchone()
            if not ev:
                return jsonify({'success': True, 'matched': False, 'reason': 'event_not_found'}), 200

            expected_key = (ev['sepay_api_key'] or '').strip()
            if not expected_key:
                return jsonify({'success': False, 'error': 'Event is not configured'}), 400
            if auth_key != expected_key:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401

            # Dedupe by SePay transaction id
            sepay_id = payload.get('id')
            try:
                sepay_id_int = int(sepay_id) if sepay_id is not None else None
            except (TypeError, ValueError):
                sepay_id_int = None

            if sepay_id_int is not None:
                try:
                    cur.execute('INSERT INTO sepay_webhook_dedupe (sepay_id, received_at) VALUES (?, ?)', (sepay_id_int, time.time()))
                    conn.commit()
                except sqlite3.IntegrityError:
                    # Duplicate delivery
                    return jsonify({'success': True, 'matched': True, 'duplicate': True}), 200

            transfer_type = sanitize_input(payload.get('transferType', ''), max_length=10).lower()
            if transfer_type and transfer_type != 'in':
                return jsonify({'success': True, 'matched': False, 'reason': 'not_incoming'}), 200

            content = sanitize_input(payload.get('content', ''), max_length=300)
            code = sanitize_input(payload.get('code', ''), max_length=120)
            ref_code = sanitize_input(payload.get('referenceCode', ''), max_length=120)

            # Try to resolve orderId
            order_id = ''
            if code:
                order_id = code
            if not order_id and content:
                m = re.search(r'(TK[0-9A-Z]{6,})', content.upper())
                if m:
                    order_id = m.group(1)

            if not order_id:
                return jsonify({'success': True, 'matched': False, 'reason': 'no_order_id'}), 200

            try:
                amt = payload.get('transferAmount')
                amount_int = int(float(amt)) if amt is not None else None
            except (TypeError, ValueError):
                amount_int = None

            cur.execute('SELECT status, total_amount FROM ticket_orders WHERE order_id = ? AND event_id = ?', (order_id, event_id))
            order = cur.fetchone()
            if not order:
                return jsonify({'success': True, 'matched': False, 'reason': 'order_not_found'}), 200

            expected_amount = int(order['total_amount'])
            if amount_int is not None and amount_int != expected_amount:
                return jsonify({'success': True, 'matched': False, 'reason': 'amount_mismatch'}), 200

            status = (order['status'] or '').lower()
            if status == 'paid':
                return jsonify({'success': True, 'matched': True, 'already_paid': True}), 200

            ticket_ids = []
            cur.execute('''
                UPDATE ticket_orders
                SET status = 'paid', paid_at = ?, sepay_transaction_id = ?, sepay_reference_code = ?
                WHERE order_id = ?
            ''', (time.time(), sepay_id_int, ref_code or None, order_id))

            cur.execute('''
                UPDATE ticket_items
                SET status = 'valid'
                WHERE order_id = ? AND status NOT IN ('used', 'cancelled')
            ''', (order_id,))

            cur.execute('SELECT ticket_id FROM ticket_items WHERE order_id = ? ORDER BY ticket_id ASC', (order_id,))
            ticket_ids = [r['ticket_id'] for r in cur.fetchall()]
            conn.commit()

            _ws_emit_tickets_updated(ticket_ids, extra={'orderId': order_id, 'orderStatus': 'paid', 'eventId': event_id})
            _ws_emit_order_updated(order_id=order_id, event_id=event_id, status='paid', ticket_ids=ticket_ids)

            return jsonify({'success': True, 'matched': True, 'orderId': order_id}), 200
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in api_sepay_webhook: {e}")
        # Return 200 with success=false to avoid SePay retry storm on server errors.
        return jsonify({'success': False, 'error': 'server_error'}), 200


@app.route('/api/download', methods=['POST'])
@limiter.limit("100 per hour; 500 per day")
def api_download():
    """API tải QR dưới dạng file (có thể kèm tiêu đề)"""
    try:
        qr_type = sanitize_input(request.form.get('qr_type', ''), max_length=30).lower()
        data = sanitize_qr_data(request.form.get('data', 'https://qrio.site'), max_length=4000)
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
        track_event(
            '/api/download',
            'download_qr',
            get_country_from_request(),
            get_device_type(),
            qr_type=qr_type,
            source=get_source_from_request(),
        )
        
        return send_file(buf, mimetype='image/png', as_attachment=True, download_name=f'{filename}.png')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/track-download', methods=['POST'])
@limiter.limit("60 per minute")
def api_track_download():
    """Track client-side export downloads (PNG/JPG/SVG/PDF) for analytics."""
    try:
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            payload = request.form.to_dict(flat=True)

        qr_type = sanitize_input(payload.get('qr_type', ''), max_length=30).lower() or None

        track_event(
            '/api/track-download',
            'download_qr',
            get_country_from_request(),
            get_device_type(),
            qr_type=qr_type,
            source=get_source_from_request(),
        )

        return jsonify({'ok': True})
    except Exception:
        return jsonify({'ok': False}), 200


@app.route('/api/file/upload', methods=['POST'])
@limiter.limit("10 per minute")
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
        logger.error(f"Lỗi thêm tiêu đề: {e}")
        return qr_img



if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') == 'development'
    host = '0.0.0.0' if not debug else 'localhost'
    
    # Production startup checks
    if not debug:
        # Verify critical env vars are set
        if not os.environ.get('ADMIN_PASSWORD_HASH'):
            logger.warning("⚠️  ADMIN_PASSWORD_HASH not set - admin login disabled!")
        if os.environ.get('SESSION_SECRET_KEY', '').startswith('dev-'):
            logger.warning("⚠️  Using development SESSION_SECRET_KEY in production!")
        
        # (No automatic admin cleanup)
    
    logger.info(f"✓ Qrio đang chạy tại http://localhost:{port}")
    logger.info("✓ Mở trình duyệt và truy cập")
    logger.info("✓ Bấn Ctrl+C để dừng server")
    if socketio:
        socketio.run(app, debug=debug, host=host, port=port)
    else:
        app.run(debug=debug, host=host, port=port)
