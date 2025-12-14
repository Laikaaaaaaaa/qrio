"""
Admin Dashboard Module - HIGH SECURITY
=====================================
Principal: READ-ONLY, ONE-ADMIN, ZERO-TRUST

Security features:
- Server-side session only (NO JWT, NO localStorage)
- HttpOnly + Secure + SameSite=Strict cookies
- Bcrypt password verification with constant-time compare
- IP-based brute-force protection
- Rate limiting on login endpoint
- No write APIs exposed
- All data pre-aggregated server-side
"""

import os
import time
import sqlite3
import secrets
import functools
from datetime import datetime, timedelta
from flask import Blueprint, request, redirect, url_for, make_response, jsonify, g
import bcrypt

# ========================
# CONFIGURATION
# ========================

# Session settings
SESSION_COOKIE_NAME = 'qrio_admin_sid'
SESSION_MAX_AGE = 1800  # 30 minutes
SESSION_PATH = '/admin'

# Brute-force protection
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 900  # 15 minutes

# Database
ANALYTICS_DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'analytics.db')
SESSION_DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'sessions.db')

def _ensure_data_dir():
    os.makedirs(os.path.dirname(SESSION_DB_PATH), exist_ok=True)


def init_session_db():
    """Initialize session/lockout database schema."""
    _ensure_data_dir()
    conn = sqlite3.connect(SESSION_DB_PATH, timeout=5)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_sessions (
                session_id TEXT PRIMARY KEY,
                created REAL NOT NULL,
                last_access REAL NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_admin_sessions_last_access ON admin_sessions(last_access)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_login_attempts (
                ip TEXT PRIMARY KEY,
                count INTEGER NOT NULL,
                first_attempt REAL NOT NULL,
                locked_until REAL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_admin_login_attempts_locked_until ON admin_login_attempts(locked_until)')
        conn.commit()
    finally:
        conn.close()


def get_session_db():
    if not os.path.exists(SESSION_DB_PATH):
        init_session_db()
    return sqlite3.connect(SESSION_DB_PATH, timeout=5)


def get_client_ip():
    """Get client IP, accounting for proxies."""
    # Trust X-Forwarded-For only in production behind trusted proxy
    if os.environ.get('FLASK_ENV') == 'production':
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def is_ip_locked(ip):
    """Check if IP is currently locked out."""
    try:
        conn = get_session_db()
        cursor = conn.cursor()
        cursor.execute('SELECT locked_until FROM admin_login_attempts WHERE ip = ?', (ip,))
        row = cursor.fetchone()
        if not row:
            return False
        locked_until = row[0]
        return bool(locked_until) and time.time() < float(locked_until)
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def record_failed_attempt(ip):
    """Record a failed login attempt."""
    now = time.time()
    conn = get_session_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT count, first_attempt, locked_until FROM admin_login_attempts WHERE ip = ?', (ip,))
        row = cursor.fetchone()

        if not row:
            cursor.execute(
                'INSERT INTO admin_login_attempts (ip, count, first_attempt, locked_until) VALUES (?, ?, ?, ?)',
                (ip, 1, now, None)
            )
            conn.commit()
            return

        count, first_attempt, locked_until = row
        first_attempt = float(first_attempt or 0)

        # Reset if window expired
        if now - first_attempt > LOCKOUT_DURATION:
            count = 1
            first_attempt = now
            locked_until = None
        else:
            count = int(count or 0) + 1
            if count >= MAX_LOGIN_ATTEMPTS:
                locked_until = now + LOCKOUT_DURATION

        cursor.execute(
            'UPDATE admin_login_attempts SET count = ?, first_attempt = ?, locked_until = ? WHERE ip = ?',
            (count, first_attempt, locked_until, ip)
        )
        conn.commit()
    finally:
        conn.close()


def clear_failed_attempts(ip):
    """Clear failed attempts on successful login."""
    try:
        conn = get_session_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM admin_login_attempts WHERE ip = ?', (ip,))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def verify_password(plain_password):
    """
    Verify password against stored hash.
    Uses bcrypt's constant-time comparison.
    """
    stored_hash = os.environ.get('ADMIN_PASSWORD_HASH', '')
    if not stored_hash:
        return False
    
    try:
        return bcrypt.checkpw(
            plain_password.encode('utf-8'),
            stored_hash.encode('utf-8')
        )
    except Exception:
        return False


def create_session():
    """Create a new server-side session."""
    session_id = secrets.token_hex(32)
    now = time.time()
    conn = get_session_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO admin_sessions (session_id, created, last_access) VALUES (?, ?, ?)',
            (session_id, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def validate_session(session_id):
    """Validate and refresh session."""
    if not session_id:
        return False

    now = time.time()
    conn = get_session_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT last_access FROM admin_sessions WHERE session_id = ?', (session_id,))
        row = cursor.fetchone()
        if not row:
            return False

        last_access = float(row[0] or 0)
        if now - last_access > SESSION_MAX_AGE:
            cursor.execute('DELETE FROM admin_sessions WHERE session_id = ?', (session_id,))
            conn.commit()
            return False

        cursor.execute('UPDATE admin_sessions SET last_access = ? WHERE session_id = ?', (now, session_id))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def destroy_session(session_id):
    """Destroy a session."""
    if not session_id:
        return
    try:
        conn = get_session_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM admin_sessions WHERE session_id = ?', (session_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cleanup_expired_sessions():
    """Remove expired sessions from store."""
    try:
        conn = get_session_db()
        cursor = conn.cursor()
        threshold = time.time() - SESSION_MAX_AGE
        cursor.execute('DELETE FROM admin_sessions WHERE last_access < ?', (threshold,))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ========================
# DATABASE INITIALIZATION
# ========================

def init_analytics_db():
    """Initialize analytics database with schema."""
    os.makedirs(os.path.dirname(ANALYTICS_DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(ANALYTICS_DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            page TEXT NOT NULL,
            event TEXT NOT NULL,
            country TEXT DEFAULT 'Unknown',
            device TEXT DEFAULT 'Unknown'
        )
    ''')
    
    # Create indexes for common queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_time ON analytics_events(time)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_event ON analytics_events(event)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_country ON analytics_events(country)')
    
    conn.commit()
    conn.close()


def get_analytics_db():
    """Get analytics database connection."""
    if not os.path.exists(ANALYTICS_DB_PATH):
        init_analytics_db()
    return sqlite3.connect(ANALYTICS_DB_PATH)


# ========================
# ANALYTICS DATA FUNCTIONS (READ-ONLY)
# ========================

def get_summary_stats():
    """Get pre-aggregated summary statistics."""
    conn = get_analytics_db()
    cursor = conn.cursor()
    
    try:
        # Total views
        cursor.execute('SELECT COUNT(*) FROM analytics_events WHERE event = ?', ('page_view',))
        total_views = cursor.fetchone()[0] or 0
        
        # Total QR generated
        cursor.execute('SELECT COUNT(*) FROM analytics_events WHERE event = ?', ('generate_qr',))
        total_generated = cursor.fetchone()[0] or 0
        
        # Total downloads
        cursor.execute('SELECT COUNT(*) FROM analytics_events WHERE event = ?', ('download_qr',))
        total_downloads = cursor.fetchone()[0] or 0
        
        # Today's views
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            'SELECT COUNT(*) FROM analytics_events WHERE event = ? AND date(time) = ?',
            ('page_view', today)
        )
        today_views = cursor.fetchone()[0] or 0
        
        # Views last 7 days (for chart)
        daily_views = []
        for i in range(6, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            cursor.execute(
                'SELECT COUNT(*) FROM analytics_events WHERE event = ? AND date(time) = ?',
                ('page_view', day)
            )
            count = cursor.fetchone()[0] or 0
            daily_views.append({'date': day, 'count': count})
        
        return {
            'total_views': total_views,
            'total_generated': total_generated,
            'total_downloads': total_downloads,
            'today_views': today_views,
            'daily_views': daily_views
        }
    finally:
        conn.close()


def get_country_stats():
    """Get pre-aggregated country statistics."""
    conn = get_analytics_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT country, COUNT(*) as count
            FROM analytics_events
            WHERE event = 'page_view' AND country IS NOT NULL
            GROUP BY country
            ORDER BY count DESC
            LIMIT 10
        ''')
        
        rows = cursor.fetchall()
        return [{'country': row[0] or 'Unknown', 'count': row[1]} for row in rows]
    finally:
        conn.close()


def get_event_stats():
    """Get pre-aggregated event statistics."""
    conn = get_analytics_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT event, COUNT(*) as count
            FROM analytics_events
            GROUP BY event
            ORDER BY count DESC
        ''')
        
        rows = cursor.fetchall()
        return [{'event': row[0], 'count': row[1]} for row in rows]
    finally:
        conn.close()


# ========================
# TRACKING FUNCTION (for use in main app)
# ========================

def track_event(page, event, country='Unknown', device='Unknown'):
    """
    Track an analytics event.
    Called from main app routes.
    Does NOT expose any write API to dashboard.
    """
    try:
        conn = get_analytics_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO analytics_events (page, event, country, device) VALUES (?, ?, ?, ?)',
            (page[:100], event[:50], country[:50], device[:50])
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Fail silently - analytics should never break main app


# ========================
# BLUEPRINT & MIDDLEWARE
# ========================

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')


def _wants_json_response() -> bool:
    accept = (request.headers.get('Accept', '') or '').lower()
    return 'application/json' in accept


def require_admin(f):
    """Decorator to require admin authentication."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        
        if not validate_session(session_id):
            return redirect(url_for('admin.login'))
        
        return f(*args, **kwargs)
    return decorated


# ========================
# ADMIN ROUTES
# ========================

@admin_bp.route('/', methods=['GET'], strict_slashes=False)
def admin_root():
    """Redirect /admin to the appropriate page."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if validate_session(session_id):
        return redirect(url_for('admin.dashboard'))
    return redirect(url_for('admin.login'))


@admin_bp.route('/login', methods=['GET'], strict_slashes=False)
def login():
    """Render login page."""
    # If already logged in, redirect to dashboard
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if validate_session(session_id):
        return redirect(url_for('admin.dashboard'))
    
    return send_login_page()


@admin_bp.route('/login', methods=['POST'], strict_slashes=False)
def login_post():
    """Handle login submission."""
    ip = get_client_ip()

    wants_json = _wants_json_response()
    
    # Check lockout
    if is_ip_locked(ip):
        msg = 'Quá nhiều lần thử. Vui lòng đợi 15 phút.'
        if wants_json:
            return jsonify({'error': msg}), 429
        return send_login_page(error=msg)
    
    password = request.form.get('password', '')
    
    # Verify password (constant-time)
    if verify_password(password):
        clear_failed_attempts(ip)
        session_id = create_session()

        if wants_json:
            response = make_response(jsonify({'ok': True, 'redirect': url_for('admin.dashboard')}))
        else:
            response = make_response(redirect(url_for('admin.dashboard')))

        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=os.environ.get('FLASK_ENV') == 'production',
            samesite='Strict',
            path='/admin'
        )
        # Also set for /analytics routes
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=os.environ.get('FLASK_ENV') == 'production',
            samesite='Strict',
            path='/analytics'
        )
        return response
    else:
        record_failed_attempt(ip)
        msg = 'Mật khẩu không chính xác.'
        if wants_json:
            return jsonify({'error': msg}), 401
        return send_login_page(error=msg)


@admin_bp.route('/logout', methods=['POST'], strict_slashes=False)
def logout():
    """Handle logout."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        destroy_session(session_id)
    
    response = make_response(redirect(url_for('admin.login')))
    response.delete_cookie(SESSION_COOKIE_NAME, path='/admin')
    response.delete_cookie(SESSION_COOKIE_NAME, path='/analytics')
    return response


@admin_bp.route('/dashboard', strict_slashes=False)
@require_admin
def dashboard():
    """Render dashboard page."""
    return send_dashboard_page()


# ========================
# ANALYTICS API (READ-ONLY)
# ========================

@analytics_bp.route('/summary')
@require_admin
def api_summary():
    """GET summary statistics."""
    cleanup_expired_sessions()
    return jsonify(get_summary_stats())


@analytics_bp.route('/countries')
@require_admin
def api_countries():
    """GET country statistics."""
    return jsonify(get_country_stats())


@analytics_bp.route('/events')
@require_admin
def api_events():
    """GET event statistics."""
    return jsonify(get_event_stats())


# ========================
# HTML PAGES - Serve from files
# ========================

def send_login_page(error=None):
    """Return login page HTML."""
    from flask import send_from_directory
    
    # Read the HTML file
    html_path = os.path.join(os.path.dirname(__file__), 'admin_login.html')
    
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        # Inject error message if present
        if error:
            error_html = f'<div class="error show" id="error-message">{error}</div>'
            html = html.replace('<div class="error" id="error-message"></div>', error_html)
        
        response = make_response(html)
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response
    except Exception as e:
        return make_response(f'Error loading login page: {e}', 500)


def send_dashboard_page():
    """Return dashboard page HTML."""
    html_path = os.path.join(os.path.dirname(__file__), 'admin_dashboard.html')
    
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        response = make_response(html)
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response
    except Exception as e:
        return make_response(f'Error loading dashboard page: {e}', 500)


# Initialize database on import
init_analytics_db()
init_session_db()
