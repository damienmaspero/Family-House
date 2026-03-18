"""
Flask application for booking.
Mobile-first web application for family house booking.
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Configuration
# Use Azure App Service's persistent storage directory in production, local directory in development
# Azure App Service: /home is persisted across deployments (mounted on Azure File Share)
# Detection: WEBSITE_SITE_NAME is a standard Azure App Service environment variable
# Local development: use current directory
if os.environ.get('WEBSITE_SITE_NAME'):
    # In Azure App Service, use /home/data for better reliability
    DATABASE_DIR = '/home/data'
    # Ensure directory exists
    try:
        os.makedirs(DATABASE_DIR, exist_ok=True)
        print(f"Database directory created/verified: {DATABASE_DIR}", file=sys.stderr)
    except (OSError, PermissionError) as e:
        # If we can't create /home/data, fall back to /home
        print(f"Warning: Could not create {DATABASE_DIR}: {e}", file=sys.stderr)
        DATABASE_DIR = '/home'
        print(f"Falling back to: {DATABASE_DIR}", file=sys.stderr)
else:
    DATABASE_DIR = '.'

DATABASE = os.path.join(DATABASE_DIR, 'bookings.db')
MAX_CAPACITY = 15  # Maximum number of people per day

def get_password_hash():
    """Get password hash from environment or use default."""
    password = os.environ.get('APP_PASSWORD', 'Pass@word123')  # Default password (should be changed in production)
    return generate_password_hash(password)


def get_db():
    """Get a database connection."""
    db = sqlite3.connect(DATABASE, timeout=10.0)
    db.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency and Azure Files compatibility
    db.execute('PRAGMA journal_mode=WAL')
    return db


def init_db():
    """Initialize the database."""
    try:
        print(f"Initializing database at: {DATABASE}", file=sys.stderr)
        
        with app.app_context():
            db = get_db()
            db.execute('''
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    guests INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    check_in_date TEXT NOT NULL,
                    check_out_date TEXT NOT NULL,
                    is_request INTEGER DEFAULT 0,
                    comment TEXT
                )
            ''')
            
            db.commit()
            db.close()
            print("Database initialized successfully", file=sys.stderr)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        # Log the error but don't prevent module from loading
        # The error will be raised when accessing database endpoints
        print(f"Warning: Failed to initialize database: {e}", file=sys.stderr)
        print(f"Database path: {DATABASE}", file=sys.stderr)
        print(f"Database directory exists: {os.path.exists(DATABASE_DIR)}", file=sys.stderr)
        print(f"Database directory writable: {os.access(DATABASE_DIR, os.W_OK)}", file=sys.stderr)


def login_required(f):
    """Decorator to protect routes requiring authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page."""
    if request.method == 'POST':
        password = request.form.get('password')
        # Check password
        if check_password_hash(get_password_hash(), password):
            session['logged_in'] = True
            return redirect(url_for('calendar'))
        else:
            flash('Incorrect password. Please contact a family member to get the password.', 'error')
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout."""
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """Redirect to the calendar."""
    return redirect(url_for('calendar'))


@app.route('/calendar')
@login_required
def calendar():
    """Main calendar page."""
    return render_template('calendar.html')


@app.route('/api/bookings', methods=['GET'])
@login_required
def get_bookings():
    """Get all bookings."""
    db = get_db()
    bookings = db.execute('SELECT * FROM bookings ORDER BY check_in_date').fetchall()
    db.close()
    
    result = []
    for booking in bookings:
        result.append({
            'id': booking['id'],
            'check_in_date': booking['check_in_date'],
            'check_out_date': booking['check_out_date'],
            'name': booking['name'],
            'guests': booking['guests'],
            'created_at': booking['created_at'],
            'is_request': bool(booking['is_request']),
            'comment': booking['comment']
        })
    
    return jsonify(result)


@app.route('/api/bookings', methods=['POST'])
@login_required
def create_booking():
    """Create a new booking."""
    data = request.get_json()
    
    check_in_date = data.get('check_in_date')
    check_out_date = data.get('check_out_date')
    name = data.get('name')
    guests = data.get('guests')
    is_request = data.get('is_request', False)  # Default to confirmed booking
    comment = data.get('comment', '')  # Optional comment field
    
    # Validation
    if not check_in_date or not check_out_date or not name or not guests:
        return jsonify({'error': 'All fields are required (check_in_date, check_out_date, name, guests)'}), 400
    
    # Validate dates
    try:
        check_in = datetime.strptime(check_in_date, '%Y-%m-%d')
        check_out = datetime.strptime(check_out_date, '%Y-%m-%d')
        
        if check_out <= check_in:
            return jsonify({'error': 'Check-out date must be after check-in date'}), 400
        
        # Check that the booking is at least 1 night
        nights = (check_out - check_in).days
        if nights < 1:
            return jsonify({'error': 'The booking must be at least one night'}), 400
            
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    try:
        guests = int(guests)
        if guests < 1 or guests > MAX_CAPACITY:
            return jsonify({'error': f'Number of guests must be between 1 and {MAX_CAPACITY}'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid number of guests'}), 400
    
    # Check capacity for each night in the range (only for confirmed bookings)
    # Request bookings don't count against capacity
    if not is_request:
        db = get_db()
        
        # Generate all nights between check_in and check_out (excluding check_out day)
        current_date = check_in
        nights_list = []
        while current_date < check_out:
            nights_list.append(current_date.strftime('%Y-%m-%d'))
            current_date += timedelta(days=1)
        
        # Check capacity for each night (excluding request bookings)
        for night in nights_list:
            existing_bookings = db.execute(
                '''SELECT SUM(guests) as total_guests FROM bookings 
                   WHERE check_in_date <= ? AND check_out_date > ? AND is_request = 0''', 
                (night, night)
            ).fetchone()
            
            total_guests = existing_bookings['total_guests'] or 0
            if total_guests + guests > MAX_CAPACITY:
                db.close()
                remaining = MAX_CAPACITY - total_guests
                return jsonify({
                    'error': f'Capacity exceeded for the night of {night}. Only {remaining} spot(s) remaining for that night.'
                }), 400
    db = get_db()
    cursor = db.execute(
        'INSERT INTO bookings (check_in_date, check_out_date, name, guests, is_request, comment) VALUES (?, ?, ?, ?, ?, ?)',
        (check_in_date, check_out_date, name, guests, 1 if is_request else 0, comment)
    )
    db.commit()
    booking_id = cursor.lastrowid
    db.close()
    
    return jsonify({
        'id': booking_id,
        'check_in_date': check_in_date,
        'check_out_date': check_out_date,
        'name': name,
        'guests': guests,
        'is_request': is_request,
        'comment': comment
    }), 201


@app.route('/api/bookings/capacity/<date>', methods=['GET'])
@login_required
def get_capacity(date):
    """Get remaining capacity for a date (night)."""
    db = get_db()
    # Count all confirmed bookings that include this night (check_in <= date < check_out)
    # Exclude request bookings
    result = db.execute(
        '''SELECT SUM(guests) as total_guests FROM bookings 
           WHERE check_in_date <= ? AND check_out_date > ? AND is_request = 0''',
        (date, date)
    ).fetchone()
    db.close()
    
    total_guests = result['total_guests'] or 0
    remaining = MAX_CAPACITY - total_guests
    
    return jsonify({
        'total_guests': total_guests,
        'remaining': remaining,
        'max_capacity': MAX_CAPACITY
    })


@app.route('/api/bookings/<int:booking_id>', methods=['PUT'])
@login_required
def update_booking(booking_id):
    """Update an existing booking."""
    data = request.get_json()
    
    check_in_date = data.get('check_in_date')
    check_out_date = data.get('check_out_date')
    name = data.get('name')
    guests = data.get('guests')
    is_request = data.get('is_request', False)
    comment = data.get('comment', '')  # Optional comment field
    
    # Validation
    if not check_in_date or not check_out_date or not name or not guests:
        return jsonify({'error': 'All fields are required (check_in_date, check_out_date, name, guests)'}), 400
    
    # Validate dates
    try:
        check_in = datetime.strptime(check_in_date, '%Y-%m-%d')
        check_out = datetime.strptime(check_out_date, '%Y-%m-%d')
        
        if check_out <= check_in:
            return jsonify({'error': 'Check-out date must be after check-in date'}), 400
        
        # Check that the booking is at least 1 night
        nights = (check_out - check_in).days
        if nights < 1:
            return jsonify({'error': 'The booking must be at least one night'}), 400
            
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    try:
        guests = int(guests)
        if guests < 1 or guests > MAX_CAPACITY:
            return jsonify({'error': f'Number of guests must be between 1 and {MAX_CAPACITY}'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid number of guests'}), 400
    
    # Check capacity for each night in the range, excluding the current booking
    # Only check capacity for confirmed bookings
    db = get_db()
    
    # Verify booking exists
    existing = db.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,)).fetchone()
    if not existing:
        db.close()
        return jsonify({'error': 'Booking not found'}), 404
    
    if not is_request:
        # Generate all nights between check_in and check_out (excluding check_out day)
        current_date = check_in
        nights_list = []
        while current_date < check_out:
            nights_list.append(current_date.strftime('%Y-%m-%d'))
            current_date += timedelta(days=1)
        
        # Check capacity for each night, excluding the current booking being updated and request bookings
        for night in nights_list:
            existing_bookings = db.execute(
                '''SELECT SUM(guests) as total_guests FROM bookings 
                   WHERE check_in_date <= ? AND check_out_date > ? AND id != ? AND is_request = 0''', 
                (night, night, booking_id)
            ).fetchone()
            
            total_guests = existing_bookings['total_guests'] or 0
            if total_guests + guests > MAX_CAPACITY:
                db.close()
                remaining = MAX_CAPACITY - total_guests
                return jsonify({
                    'error': f'Capacity exceeded for the night of {night}. Only {remaining} spot(s) remaining for that night.'
                }), 400
    
    # Update booking
    db.execute(
        '''UPDATE bookings 
           SET check_in_date = ?, check_out_date = ?, name = ?, guests = ?, is_request = ?, comment = ?
           WHERE id = ?''',
        (check_in_date, check_out_date, name, guests, 1 if is_request else 0, comment, booking_id)
    )
    db.commit()
    db.close()
    
    return jsonify({
        'id': booking_id,
        'check_in_date': check_in_date,
        'check_out_date': check_out_date,
        'name': name,
        'guests': guests,
        'is_request': is_request,
        'comment': comment
    })


@app.route('/api/bookings/<int:booking_id>', methods=['DELETE'])
@login_required
def delete_booking(booking_id):
    """Delete a booking."""
    db = get_db()
    db.execute('DELETE FROM bookings WHERE id = ?', (booking_id,))
    db.commit()
    db.close()
    
    return jsonify({'success': True})


# Initialize database when module is loaded
init_db()


if __name__ == '__main__':
    # Debug mode should be disabled in production
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
