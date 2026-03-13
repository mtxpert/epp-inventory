#!/usr/bin/env python3
"""EPP Inventory Manager — Cloud-deployed inventory & BOM system."""
import os
import json
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail
from flask_apscheduler import APScheduler
from models import db, User, Component, Kit, KitComponent, InventoryLog, ShopifyOrder

mail = Mail()
scheduler = APScheduler()
login_manager = LoginManager()


def create_app():
    app = Flask(__name__)

    # Config
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///inventory.db')
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    if 'postgresql' in database_url and 'sslmode' not in database_url:
        separator = '&' if '?' in database_url else '?'
        database_url += f'{separator}sslmode=require'

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    app.config['SHOPIFY_TOKEN'] = os.environ.get('SHOPIFY_TOKEN', '')
    app.config['SHOPIFY_STORE'] = os.environ.get('SHOPIFY_STORE', 'edf236-3.myshopify.com')
    app.config['SHOPIFY_WEBHOOK_SECRET'] = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')
    app.config['ALERT_RECIPIENTS'] = os.environ.get('ALERT_RECIPIENTS', 'info@ecopowerparts.com')
    app.config['APP_URL'] = os.environ.get('APP_URL', 'https://epp-inventory.onrender.com')

    # Mail config
    app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS'] = True
    app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
    app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'info@ecopowerparts.com')

    # Scheduler config
    app.config['SCHEDULER_API_ENABLED'] = False
    app.config['JOBS'] = [
        {
            'id': 'sync_orders',
            'func': 'app:scheduled_sync',
            'trigger': 'interval',
            'hours': 2,
            'misfire_grace_time': 900
        },
        {
            'id': 'daily_stock_check',
            'func': 'app:scheduled_stock_alert',
            'trigger': 'cron',
            'hour': 8,
            'minute': 0,
            'misfire_grace_time': 3600
        }
    ]

    db.init_app(app)
    mail.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message_category = 'info'

    with app.app_context():
        db.create_all()
        from seed_data import seed_database
        seed_database()
        # Create default admin if no users exist
        if not User.query.first():
            admin = User(email='info@ecopowerparts.com', name='Mike', role='admin')
            admin.set_password(os.environ.get('ADMIN_PASSWORD', 'changeme123'))
            db.session.add(admin)
            db.session.commit()

    scheduler.init_app(app)
    scheduler.start()

    register_routes(app)
    return app


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def scheduled_sync():
    """Background job: sync Shopify orders every 2 hours."""
    from shopify_sync import sync_recent_orders
    with app.app_context():
        result = sync_recent_orders(hours=3)
        app.logger.info(f"Scheduled sync: {result}")


def scheduled_stock_alert():
    """Daily 8AM stock check and email alert."""
    from shopify_sync import get_low_stock_components, send_low_stock_alert
    with app.app_context():
        low = get_low_stock_components()
        if low:
            send_low_stock_alert(low)
            app.logger.info(f"Daily alert: {len(low)} low stock items")


def register_routes(app):

    # ── Auth Routes ──────────────────────────────────────────────

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                login_user(user, remember=True)
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            flash('Invalid email or password', 'error')
        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('login'))

    # ── Password Change ─────────────────────────────────────────

    @app.route('/change-password', methods=['POST'])
    @login_required
    def change_password():
        data = request.get_json()
        old_pw = data.get('old_password', '')
        new_pw = data.get('new_password', '')
        if not current_user.check_password(old_pw):
            return jsonify({'error': 'Current password incorrect'}), 400
        if len(new_pw) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400
        current_user.set_password(new_pw)
        db.session.commit()
        return jsonify({'ok': True})

    # ── Admin Routes ─────────────────────────────────────────────

    @app.route('/admin/users')
    @login_required
    def admin_users():
        if current_user.role != 'admin':
            flash('Admin access required', 'error')
            return redirect(url_for('dashboard'))
        users = User.query.all()
        return render_template('admin_users.html', users=users)

    @app.route('/admin/users/add', methods=['POST'])
    @login_required
    def add_user():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        email = request.form.get('email', '').strip().lower()
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        if User.query.filter_by(email=email).first():
            flash('Email already exists', 'error')
            return redirect(url_for('admin_users'))
        user = User(email=email, name=name, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f'User {name} added', 'success')
        return redirect(url_for('admin_users'))

    @app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
    @login_required
    def delete_user(user_id):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash("Can't delete yourself", 'error')
            return redirect(url_for('admin_users'))
        db.session.delete(user)
        db.session.commit()
        flash(f'User {user.name} deleted', 'success')
        return redirect(url_for('admin_users'))

    # ── Dashboard ────────────────────────────────────────────────

    @app.route('/')
    @login_required
    def dashboard():
        components = Component.query.order_by(Component.category, Component.part_number).all()
        kits = Kit.query.order_by(Kit.name).all()
        low_stock = [c for c in components if c.qty <= c.reorder_threshold]
        total_stock = sum(c.qty for c in components)

        # Calculate min buildable for each kit
        kit_buildable = {}
        for kit in kits:
            if kit.components:
                kit_buildable[kit.id] = min(
                    kc.component.qty // kc.quantity for kc in kit.components
                )
            else:
                kit_buildable[kit.id] = 0

        min_buildable = min(kit_buildable.values()) if kit_buildable else 0

        # Group components by category
        categories = {}
        cat_labels = {'pipes': 'Pipes', 'couplers': 'Silicone Hoses & Couplers', 'clamps': 'Clamps', 'misc': 'Misc / Hardware'}
        for c in components:
            cat = cat_labels.get(c.category, c.category)
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(c)

        # Build "used in" map
        used_in = {}
        for kit in kits:
            for kc in kit.components:
                if kc.component.part_number not in used_in:
                    used_in[kc.component.part_number] = []
                used_in[kc.component.part_number].append(f"{kit.name} (x{kc.quantity})")

        recent_orders = ShopifyOrder.query.order_by(ShopifyOrder.created_at.desc()).limit(20).all()
        recent_logs = InventoryLog.query.order_by(InventoryLog.created_at.desc()).limit(50).all()

        return render_template('dashboard.html',
            components=components, kits=kits, categories=categories,
            low_stock=low_stock, total_stock=total_stock,
            kit_buildable=kit_buildable, min_buildable=min_buildable,
            used_in=used_in, recent_orders=recent_orders, recent_logs=recent_logs)

    # ── Inventory API ────────────────────────────────────────────

    @app.route('/api/adjust', methods=['POST'])
    @login_required
    def adjust_stock():
        data = request.get_json()
        pn = data.get('part_number')
        change = int(data.get('change', 0))
        reason = data.get('reason', 'Manual adjustment')

        comp = Component.query.filter_by(part_number=pn).first()
        if not comp:
            return jsonify({'error': 'Component not found'}), 404

        comp.qty += change
        log = InventoryLog(
            component_id=comp.id, qty_change=change,
            reason=reason, user_id=current_user.id
        )
        db.session.add(log)
        db.session.commit()
        return jsonify({'ok': True, 'qty': comp.qty})

    @app.route('/api/set', methods=['POST'])
    @login_required
    def set_stock():
        data = request.get_json()
        pn = data.get('part_number')
        new_qty = int(data.get('qty', 0))

        comp = Component.query.filter_by(part_number=pn).first()
        if not comp:
            return jsonify({'error': 'Component not found'}), 404

        old_qty = comp.qty
        comp.qty = new_qty
        log = InventoryLog(
            component_id=comp.id, qty_change=new_qty - old_qty,
            reason=f"Set to {new_qty} (was {old_qty})", user_id=current_user.id
        )
        db.session.add(log)
        db.session.commit()
        return jsonify({'ok': True, 'qty': comp.qty})

    @app.route('/api/threshold', methods=['POST'])
    @login_required
    def set_threshold():
        data = request.get_json()
        pn = data.get('part_number')
        threshold = int(data.get('threshold', 10))
        comp = Component.query.filter_by(part_number=pn).first()
        if not comp:
            return jsonify({'error': 'Component not found'}), 404
        comp.reorder_threshold = threshold
        db.session.commit()
        return jsonify({'ok': True, 'threshold': comp.reorder_threshold})

    @app.route('/api/sync-orders', methods=['POST'])
    @login_required
    def sync_orders():
        from shopify_sync import sync_recent_orders
        result = sync_recent_orders(hours=24)
        return jsonify(result)

    # ── Kit CRUD ─────────────────────────────────────────────────

    @app.route('/api/kit', methods=['POST'])
    @login_required
    def create_kit():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        data = request.get_json()
        slug = data.get('slug', '').strip().lower().replace(' ', '_')
        name = data.get('name', '').strip()
        shopify_id = data.get('shopify_id', '').strip() or None
        shopify_variant = data.get('shopify_variant', '').strip() or None

        if Kit.query.filter_by(slug=slug).first():
            return jsonify({'error': 'Kit slug already exists'}), 400

        kit = Kit(slug=slug, name=name, shopify_id=shopify_id, shopify_variant=shopify_variant)
        db.session.add(kit)
        db.session.flush()

        for pn, qty in data.get('components', {}).items():
            comp = Component.query.filter_by(part_number=pn).first()
            if comp:
                kc = KitComponent(kit_id=kit.id, component_id=comp.id, quantity=int(qty))
                db.session.add(kc)

        db.session.commit()
        return jsonify({'ok': True, 'kit_id': kit.id})

    @app.route('/api/kit/<int:kit_id>', methods=['PUT'])
    @login_required
    def update_kit(kit_id):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        kit = Kit.query.get_or_404(kit_id)
        data = request.get_json()

        kit.name = data.get('name', kit.name)
        kit.shopify_id = data.get('shopify_id', kit.shopify_id) or None
        kit.shopify_variant = data.get('shopify_variant', kit.shopify_variant) or None

        if 'components' in data:
            KitComponent.query.filter_by(kit_id=kit.id).delete()
            for pn, qty in data['components'].items():
                comp = Component.query.filter_by(part_number=pn).first()
                if comp:
                    kc = KitComponent(kit_id=kit.id, component_id=comp.id, quantity=int(qty))
                    db.session.add(kc)

        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/kit/<int:kit_id>', methods=['DELETE'])
    @login_required
    def delete_kit(kit_id):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        kit = Kit.query.get_or_404(kit_id)
        db.session.delete(kit)
        db.session.commit()
        return jsonify({'ok': True})

    # ── Shopify Webhook ──────────────────────────────────────────

    @app.route('/webhook/shopify/order', methods=['POST'])
    def shopify_order_webhook():
        """Receive Shopify order webhook — no auth required, HMAC verified."""
        from shopify_sync import verify_webhook, process_order, get_low_stock_components, send_low_stock_alert

        data = request.get_data()
        hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
        secret = app.config.get('SHOPIFY_WEBHOOK_SECRET')

        if secret and not verify_webhook(data, hmac_header, secret):
            return jsonify({'error': 'Invalid signature'}), 401

        order_data = request.get_json()
        if not order_data:
            return jsonify({'error': 'No data'}), 400

        result = process_order(order_data)

        # Check for low stock after processing
        if result.get('deductions'):
            low = get_low_stock_components()
            if low:
                send_low_stock_alert(low)

        return jsonify(result), 200

    # ── Health Check ─────────────────────────────────────────────

    @app.route('/health')
    def health():
        try:
            count = Component.query.count()
            return jsonify({'status': 'ok', 'components': count})
        except Exception as e:
            return jsonify({'status': 'error', 'error': str(e)}), 500


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
