#!/usr/bin/env python3
"""EPP Inventory Manager — Cloud-deployed inventory & BOM system."""
import os
import io
import csv
import json
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, Response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail
from flask_apscheduler import APScheduler
from models import (db, User, Component, Kit, KitComponent, InventoryLog, ShopifyOrder,
                     Supplier, SupplierComponent, PurchaseOrder, PurchaseOrderLine,
                     InventorySnapshot, Invoice, InvoiceLine)

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
        },
        {
            'id': 'year_end_inventory',
            'func': 'app:scheduled_year_end_snapshot',
            'trigger': 'cron',
            'month': 12,
            'day': 31,
            'hour': 23,
            'minute': 30,
            'misfire_grace_time': 86400
        }
    ]

    db.init_app(app)
    mail.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message_category = 'info'

    with app.app_context():
        db.create_all()
        # Add retail_price column if missing
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(db.engine)
        kit_cols = [c['name'] for c in inspector.get_columns('kits')]
        if 'retail_price' not in kit_cols:
            db.session.execute(db.text("ALTER TABLE kits ADD COLUMN retail_price FLOAT DEFAULT 0"))
            db.session.commit()
        # Add unit_cost column to components if missing (must be before any Component queries)
        comp_cols = [c['name'] for c in inspector.get_columns('components')]
        if 'unit_cost' not in comp_cols:
            db.session.execute(db.text("ALTER TABLE components ADD COLUMN unit_cost FLOAT DEFAULT 0"))
            db.session.commit()
        from seed_data import seed_database
        seed_database()
        # Update kit prices from seed data
        from seed_data import KITS
        for slug, kit_info in KITS.items():
            kit = Kit.query.filter_by(slug=slug).first()
            if kit and kit_info.get('retail_price') and (not kit.retail_price):
                kit.retail_price = kit_info['retail_price']
        db.session.commit()
        # Seed suppliers if none exist
        if not Supplier.query.first():
            suppliers_data = [
                {'name': 'R and I', 'email': 'elena@rimetal.com', 'contact_name': 'Elena',
                 'notes': 'Intake heat shields', 'parts': ['IN-HEAT']},
                {'name': 'Performance Tube Bending', 'email': 'mike@rsmetals.us', 'contact_name': 'Mike',
                 'notes': 'All pipes', 'category': 'pipes', 'exclude_pns': ['IN-HEAT']},
                {'name': 'R-EP Auto Parts', 'email': 'repautoparts@r-ep.com', 'contact_name': '',
                 'notes': 'All hoses/couplers', 'category': 'couplers'},
            ]
            for sd in suppliers_data:
                s = Supplier(name=sd['name'], email=sd['email'],
                             contact_name=sd.get('contact_name', ''), notes=sd.get('notes', ''))
                db.session.add(s)
                db.session.flush()
                if 'parts' in sd:
                    for pn in sd['parts']:
                        comp = Component.query.filter_by(part_number=pn).first()
                        if comp:
                            db.session.add(SupplierComponent(supplier_id=s.id, component_id=comp.id))
                elif 'category' in sd:
                    exclude = set(sd.get('exclude_pns', []))
                    for comp in Component.query.filter_by(category=sd['category']).all():
                        if comp.part_number not in exclude:
                            db.session.add(SupplierComponent(supplier_id=s.id, component_id=comp.id))
            db.session.commit()
        # Fix: remove IN-HEAT from PTB (belongs to R&I only)
        ptb = Supplier.query.filter_by(name='Performance Tube Bending').first()
        in_heat = Component.query.filter_by(part_number='IN-HEAT').first()
        if ptb and in_heat:
            bad = SupplierComponent.query.filter_by(supplier_id=ptb.id, component_id=in_heat.id).first()
            if bad:
                db.session.delete(bad)
                db.session.commit()
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


def generate_inventory_snapshot(email_to=None):
    """Generate inventory valuation snapshot and optionally email it."""
    from flask_mail import Message
    import json
    from datetime import date

    components = Component.query.all()
    kits = Kit.query.all()

    # Build kit cost: sum of component unit_costs * qty per kit
    details = []
    total_retail = 0
    total_cost = 0

    for comp in components:
        # Get cost from latest invoice or supplier_component
        cost = comp.unit_cost or 0
        if not cost:
            sc = SupplierComponent.query.filter_by(component_id=comp.id).first()
            if sc:
                cost = sc.unit_cost or 0

        comp_value_cost = comp.qty * cost
        total_cost += comp_value_cost

        details.append({
            'part_number': comp.part_number,
            'name': comp.name,
            'qty': comp.qty,
            'unit_cost': cost,
            'total_cost': round(comp_value_cost, 2),
        })

    # Calculate retail value (pipe-limited buildable * retail price)
    pipe_stock = {c.id: c.qty for c in components if c.category == 'pipes'}
    kit_priority = {
        'hot_pipes_sho': 1, 'intake_stock_hose': 2, 'intake_custom_hose': 3,
        'hot_pipes_explorer': 4, 'fusion_intake': 5, 'fusion_charge': 6,
        'f150_intake': 7, 'nmd_upgrade': 8, 'nmd': 9, 'explorer_nmd': 10,
    }
    kits_by_priority = sorted(kits, key=lambda k: kit_priority.get(k.slug, 99))
    kit_retail_details = []
    for kit in kits_by_priority:
        pipe_parts = [kc for kc in kit.components if kc.component.category == 'pipes']
        if not pipe_parts:
            continue
        max_build = min(pipe_stock.get(kc.component_id, 0) // kc.quantity for kc in pipe_parts)
        for kc in pipe_parts:
            pipe_stock[kc.component_id] = pipe_stock.get(kc.component_id, 0) - (kc.quantity * max_build)
        kit_value = max_build * (kit.retail_price or 0)
        total_retail += kit_value
        if max_build > 0:
            kit_retail_details.append({
                'kit': kit.name, 'buildable': max_build,
                'retail_price': kit.retail_price, 'total': round(kit_value, 2)
            })

    snapshot_date = date.today()
    snap = InventorySnapshot(
        snapshot_date=snapshot_date,
        total_retail_value=round(total_retail, 2),
        total_cost_value=round(total_cost, 2),
        details_json=json.dumps({
            'components': details,
            'kits': kit_retail_details,
        }),
        emailed_to=email_to or ''
    )
    db.session.add(snap)
    db.session.commit()

    # Build email
    body = f"EPP INVENTORY VALUATION — {snapshot_date.strftime('%B %d, %Y')}\n"
    body += "=" * 60 + "\n\n"
    body += f"Total Retail Value (pipe-limited kits): ${total_retail:,.2f}\n"
    body += f"Total Cost Basis (component inventory):  ${total_cost:,.2f}\n"
    body += f"Estimated Gross Margin:                  ${total_retail - total_cost:,.2f}\n\n"

    body += "KIT BUILDABLE INVENTORY (retail)\n"
    body += "-" * 55 + "\n"
    body += f"{'Kit':<35} {'Qty':>5} {'Price':>8} {'Total':>10}\n"
    body += "-" * 55 + "\n"
    for kd in kit_retail_details:
        body += f"{kd['kit']:<35} {kd['buildable']:>5} ${kd['retail_price']:>7,.0f} ${kd['total']:>9,.2f}\n"
    body += "-" * 55 + "\n"
    body += f"{'TOTAL':<35} {'':>5} {'':>8} ${total_retail:>9,.2f}\n\n"

    body += "COMPONENT INVENTORY (at cost)\n"
    body += "-" * 65 + "\n"
    body += f"{'Part #':<15} {'Name':<30} {'Qty':>5} {'Cost':>8} {'Total':>10}\n"
    body += "-" * 65 + "\n"
    for d in sorted(details, key=lambda x: x['part_number']):
        if d['qty'] > 0:
            body += f"{d['part_number']:<15} {d['name'][:30]:<30} {d['qty']:>5} ${d['unit_cost']:>7.2f} ${d['total_cost']:>9.2f}\n"
    body += "-" * 65 + "\n"
    body += f"{'TOTAL':<15} {'':>30} {'':>5} {'':>8} ${total_cost:>9,.2f}\n"

    if email_to:
        try:
            msg = Message(
                subject=f"[EPP] Inventory Valuation — {snapshot_date.strftime('%Y-%m-%d')}",
                recipients=[e.strip() for e in email_to.split(',')],
                body=body
            )
            mail.send(msg)
        except Exception as e:
            app.logger.error(f"Failed to email inventory snapshot: {e}")

    return {'snapshot_id': snap.id, 'total_retail': total_retail, 'total_cost': total_cost, 'email_body': body}


def scheduled_year_end_snapshot():
    """12/31 at 11:30pm — snapshot inventory and email to accountant."""
    with app.app_context():
        result = generate_inventory_snapshot(email_to='sean@askwold.com,info@ecopowerparts.com')
        app.logger.info(f"Year-end snapshot: retail=${result['total_retail']:,.2f}, cost=${result['total_cost']:,.2f}")


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

        # Calculate min buildable for each kit (all components)
        kit_buildable = {}
        for kit in kits:
            if kit.components:
                kit_buildable[kit.id] = min(
                    kc.component.qty // kc.quantity for kc in kit.components
                )
            else:
                kit_buildable[kit.id] = 0

        min_buildable = min(kit_buildable.values()) if kit_buildable else 0

        # Priority-based PIPE-ONLY buildable (clamps/hoses are commodity)
        # Priority: SHO Hot Pipes → Intakes → Explorer Hot → Fusion → F150 → NMD leftovers
        kit_priority = {
            'hot_pipes_sho': 1,
            'intake_stock_hose': 2,
            'intake_custom_hose': 3,
            'hot_pipes_explorer': 4,
            'fusion_intake': 5,
            'fusion_charge': 6,
            'f150_intake': 7,
            'nmd_upgrade': 8,
            'nmd': 9,
            'explorer_nmd': 10,
        }
        pipe_stock = {c.id: c.qty for c in components if c.category == 'pipes'}
        kits_by_priority = sorted(kits, key=lambda k: kit_priority.get(k.slug, 99))
        kit_pipe_buildable = {}
        for kit in kits_by_priority:
            pipe_parts = [kc for kc in kit.components if kc.component.category == 'pipes']
            if not pipe_parts:
                kit_pipe_buildable[kit.id] = 0
                continue
            max_build = min(pipe_stock.get(kc.component_id, 0) // kc.quantity for kc in pipe_parts)
            kit_pipe_buildable[kit.id] = max_build
            for kc in pipe_parts:
                pipe_stock[kc.component_id] = pipe_stock.get(kc.component_id, 0) - (kc.quantity * max_build)

        total_retail_value = sum(
            kit_pipe_buildable.get(kit.id, 0) * (kit.retail_price or 0)
            for kit in kits if kit_pipe_buildable.get(kit.id, 0) > 0
        )

        # Group components by category (ordered)
        cat_order = ['pipes', 'couplers', 'clamps', 'misc']
        cat_labels = {'pipes': 'Pipes', 'couplers': 'Silicone Hoses & Couplers', 'clamps': 'Clamps', 'misc': 'Misc / Hardware'}
        categories = {}
        for cat_key in cat_order:
            label = cat_labels.get(cat_key, cat_key)
            cat_comps = [c for c in components if c.category == cat_key]
            if cat_comps:
                categories[label] = cat_comps

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
            kit_pipe_buildable=kit_pipe_buildable,
            total_retail_value=total_retail_value,
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

    # ── CSV Export ─────────────────────────────────────────────

    @app.route('/api/export-csv')
    @login_required
    def export_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Part Number', 'Name', 'Category', 'Old PN', 'Qty', 'Reorder At', 'Status'])
        components = Component.query.order_by(Component.category, Component.part_number).all()
        for c in components:
            status = 'OUT OF STOCK' if c.qty <= 0 else 'Low' if c.qty <= c.reorder_threshold else 'OK'
            writer.writerow([c.part_number, c.name, c.category, c.old_pn, c.qty, c.reorder_threshold, status])
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename=epp-inventory-{datetime.now().strftime("%Y%m%d")}.csv'}
        )

    # ── Build Kit (manual deduction) ──────────────────────────

    @app.route('/api/build-kit', methods=['POST'])
    @login_required
    def build_kit():
        data = request.get_json()
        kit_id = data.get('kit_id')
        qty = int(data.get('qty', 1))
        kit = Kit.query.get(kit_id)
        if not kit:
            return jsonify({'error': 'Kit not found'}), 404

        # Check if buildable
        for kc in kit.components:
            needed = kc.quantity * qty
            if kc.component.qty < needed:
                return jsonify({
                    'error': f'Not enough {kc.component.part_number}: need {needed}, have {kc.component.qty}'
                }), 400

        # Deduct
        deductions = []
        for kc in kit.components:
            total_deduct = kc.quantity * qty
            kc.component.qty -= total_deduct
            log = InventoryLog(
                component_id=kc.component_id,
                qty_change=-total_deduct,
                reason=f"Manual build: {kit.name} x{qty}",
                user_id=current_user.id
            )
            db.session.add(log)
            deductions.append({
                'part': kc.component.part_number,
                'deducted': total_deduct,
                'remaining': kc.component.qty
            })

        db.session.commit()
        return jsonify({'ok': True, 'deductions': deductions})

    # ── Component CRUD (admin) ────────────────────────────────

    @app.route('/api/component', methods=['POST'])
    @login_required
    def add_component():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        data = request.get_json()
        pn = data.get('part_number', '').strip().upper()
        name = data.get('name', '').strip()
        category = data.get('category', 'misc').strip().lower()
        qty = int(data.get('qty', 0))
        threshold = int(data.get('reorder_threshold', 10))

        if not pn or not name:
            return jsonify({'error': 'Part number and name required'}), 400
        if Component.query.filter_by(part_number=pn).first():
            return jsonify({'error': 'Part number already exists'}), 400

        comp = Component(
            part_number=pn, name=name, category=category,
            qty=qty, reorder_threshold=threshold
        )
        db.session.add(comp)
        log = InventoryLog(
            component_id=None, qty_change=qty,
            reason=f"New component added: {pn}", user_id=current_user.id
        )
        db.session.flush()
        log.component_id = comp.id
        db.session.add(log)
        db.session.commit()
        return jsonify({'ok': True, 'id': comp.id})

    @app.route('/api/component/<part_number>', methods=['DELETE'])
    @login_required
    def delete_component(part_number):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        comp = Component.query.filter_by(part_number=part_number).first()
        if not comp:
            return jsonify({'error': 'Not found'}), 404
        # Check if used in any kit
        if comp.kit_components:
            kit_names = [kc.kit.name for kc in comp.kit_components]
            return jsonify({'error': f'Used in kits: {", ".join(kit_names)}'}), 400
        InventoryLog.query.filter_by(component_id=comp.id).delete()
        db.session.delete(comp)
        db.session.commit()
        return jsonify({'ok': True})

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

    # ── Purchase Orders ──────────────────────────────────────────

    @app.route('/orders')
    @login_required
    def purchase_orders():
        suppliers = Supplier.query.order_by(Supplier.name).all()
        pos = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).all()
        return render_template('purchase_orders.html', suppliers=suppliers, purchase_orders=pos)

    @app.route('/api/po/create', methods=['POST'])
    @login_required
    def create_po():
        data = request.get_json()
        supplier_id = data.get('supplier_id')
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        # Generate PO number
        count = PurchaseOrder.query.count() + 1
        po_number = f"EPP-PO-{count:04d}"

        po = PurchaseOrder(
            po_number=po_number, supplier_id=supplier_id,
            notes=data.get('notes', ''), created_by=current_user.id
        )
        db.session.add(po)
        db.session.flush()

        for line in data.get('lines', []):
            comp = Component.query.filter_by(part_number=line['part_number']).first()
            if comp:
                pol = PurchaseOrderLine(
                    po_id=po.id, component_id=comp.id,
                    qty=int(line['qty']), unit_cost=float(line.get('unit_cost', 0))
                )
                db.session.add(pol)

        db.session.commit()
        return jsonify({'ok': True, 'po_id': po.id, 'po_number': po_number})

    @app.route('/api/po/<int:po_id>/send', methods=['POST'])
    @login_required
    def send_po(po_id):
        from flask_mail import Message
        po = PurchaseOrder.query.get_or_404(po_id)

        body = f"Purchase Order: {po.po_number}\n"
        body += f"Date: {po.created_at.strftime('%Y-%m-%d')}\n"
        body += f"From: EcoPowerParts\n"
        body += "=" * 50 + "\n\n"
        body += f"{'Part Number':<15} {'Description':<40} {'Qty':>6}\n"
        body += "-" * 65 + "\n"
        for line in po.lines:
            body += f"{line.component.part_number:<15} {line.component.name:<40} {line.qty:>6}\n"
        body += "-" * 65 + "\n"
        if po.notes:
            body += f"\nNotes: {po.notes}\n"
        body += f"\nPlease confirm receipt of this order.\nThank you,\nEcoPowerParts\n"

        try:
            msg = Message(
                subject=f"[EPP] Purchase Order {po.po_number}",
                recipients=[po.supplier.email],
                body=body
            )
            mail.send(msg)
            po.status = 'sent'
            po.sent_at = datetime.now(timezone.utc)
            db.session.commit()
            return jsonify({'ok': True, 'email_body': body})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/po/<int:po_id>/receive', methods=['POST'])
    @login_required
    def receive_po(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        po.status = 'received'
        po.received_at = datetime.now(timezone.utc)
        # Add received quantities to inventory
        for line in po.lines:
            line.component.qty += line.qty
            log = InventoryLog(
                component_id=line.component_id, qty_change=line.qty,
                reason=f"PO {po.po_number} received", user_id=current_user.id
            )
            db.session.add(log)
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/po/<int:po_id>/cancel', methods=['POST'])
    @login_required
    def cancel_po(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        po.status = 'cancelled'
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/po/rfq', methods=['POST'])
    @login_required
    def send_rfq():
        """Send RFQ email to supplier requesting pricing at multiple qty breaks."""
        from flask_mail import Message
        data = request.get_json()
        supplier_id = data.get('supplier_id')
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        qty_breaks = data.get('qty_breaks', [25, 50, 100])
        part_numbers = data.get('part_numbers', [])

        parts = []
        for pn in part_numbers:
            comp = Component.query.filter_by(part_number=pn).first()
            if comp:
                parts.append(comp)

        if not parts:
            return jsonify({'error': 'No parts selected'}), 400

        body = f"Request for Quote\n"
        body += f"From: EcoPowerParts\n"
        body += f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        body += "=" * 60 + "\n\n"
        body += f"Hi{(' ' + supplier.contact_name) if supplier.contact_name else ''},\n\n"
        body += "We'd like to get pricing on the following parts at the quantities listed below.\n"
        body += "Please reply with your best pricing and lead time for each.\n\n"

        # Header
        qty_header = "".join(f"{'Qty ' + str(q):>12}" for q in qty_breaks)
        body += f"{'Part Number':<15} {'Description':<35}{qty_header}\n"
        body += "-" * (50 + 12 * len(qty_breaks)) + "\n"

        for comp in parts:
            qty_cols = "".join(f"{'________':>12}" for _ in qty_breaks)
            body += f"{comp.part_number:<15} {comp.name:<35}{qty_cols}\n"

        body += "-" * (50 + 12 * len(qty_breaks)) + "\n"
        body += "\nPlease include:\n"
        body += "- Unit price at each quantity\n"
        body += "- Lead time\n"
        body += "- Any minimum order requirements\n"
        body += f"\nThank you,\nMike Bambic\nEcoPowerParts\ninfo@ecopowerparts.com\n"

        try:
            msg = Message(
                subject=f"[EPP] Request for Quote — {len(parts)} parts",
                recipients=[supplier.email],
                body=body
            )
            mail.send(msg)
            return jsonify({'ok': True, 'email_body': body})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/supplier/<int:supplier_id>/components')
    @login_required
    def supplier_components(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        parts = []
        for sc in supplier.components:
            c = sc.component
            parts.append({
                'part_number': c.part_number, 'name': c.name,
                'qty': c.qty, 'reorder_threshold': c.reorder_threshold,
                'unit_cost': sc.unit_cost,
                'needs_reorder': c.qty <= c.reorder_threshold
            })
        return jsonify(parts)

    @app.route('/api/kit-part-calc', methods=['POST'])
    @login_required
    def kit_part_calc():
        """Calculate total parts needed for a given number of kits, filtered by supplier."""
        data = request.get_json()
        kit_qtys = data.get('kit_qtys', {})  # {kit_slug: qty}
        supplier_id = data.get('supplier_id')

        # Get supplier's part numbers
        supplier_pns = set()
        if supplier_id:
            for sc in SupplierComponent.query.filter_by(supplier_id=supplier_id).all():
                supplier_pns.add(sc.component_id)

        totals = {}
        for slug, qty in kit_qtys.items():
            if qty <= 0:
                continue
            kit = Kit.query.filter_by(slug=slug).first()
            if not kit:
                continue
            for kc in kit.components:
                # Filter: only parts this supplier provides
                if supplier_id and kc.component_id not in supplier_pns:
                    continue
                pn = kc.component.part_number
                if pn not in totals:
                    totals[pn] = {
                        'name': kc.component.name,
                        'in_stock': kc.component.qty,
                        'total_needed': 0,
                        'breakdown': []
                    }
                needed = kc.quantity * qty
                totals[pn]['total_needed'] += needed
                totals[pn]['breakdown'].append(f"{kit.name} x{qty} = {needed}")

        result = []
        for pn, info in sorted(totals.items()):
            info['part_number'] = pn
            result.append(info)
        return jsonify(result)

    # ── Invoices & COGS ─────────────────────────────────────────

    @app.route('/invoices')
    @login_required
    def invoices_page():
        suppliers = Supplier.query.order_by(Supplier.name).all()
        invoices = Invoice.query.order_by(Invoice.invoice_date.desc()).all()
        components = Component.query.order_by(Component.part_number).all()
        snapshots = InventorySnapshot.query.order_by(InventorySnapshot.snapshot_date.desc()).limit(10).all()

        # Margin analysis: kit retail vs component cost
        kits = Kit.query.all()
        margin_data = []
        for kit in kits:
            kit_cost = 0
            for kc in kit.components:
                comp_cost = kc.component.unit_cost or 0
                if not comp_cost:
                    sc = SupplierComponent.query.filter_by(component_id=kc.component_id).first()
                    comp_cost = sc.unit_cost if sc else 0
                kit_cost += kc.quantity * comp_cost
            margin = (kit.retail_price or 0) - kit_cost if kit_cost > 0 else None
            margin_pct = (margin / (kit.retail_price or 1) * 100) if margin is not None and kit.retail_price else None
            margin_data.append({
                'kit': kit, 'cost': round(kit_cost, 2),
                'margin': round(margin, 2) if margin is not None else None,
                'margin_pct': round(margin_pct, 1) if margin_pct is not None else None,
            })

        return render_template('invoices.html',
            suppliers=suppliers, invoices=invoices, components=components,
            snapshots=snapshots, margin_data=margin_data)

    @app.route('/api/invoice/create', methods=['POST'])
    @login_required
    def create_invoice():
        import base64
        data = request.form
        supplier_id = data.get('supplier_id')
        invoice_number = data.get('invoice_number', '').strip()
        invoice_date = data.get('invoice_date')
        notes = data.get('notes', '')

        if not supplier_id or not invoice_number or not invoice_date:
            flash('Supplier, invoice number, and date are required', 'error')
            return redirect(url_for('invoices_page'))

        inv = Invoice(
            supplier_id=int(supplier_id),
            invoice_number=invoice_number,
            invoice_date=datetime.strptime(invoice_date, '%Y-%m-%d').date(),
            notes=notes,
            created_by=current_user.id
        )

        # Handle file upload
        file = request.files.get('invoice_file')
        if file and file.filename:
            inv.file_name = file.filename
            inv.file_data = base64.b64encode(file.read()).decode('utf-8')

        db.session.add(inv)
        db.session.flush()

        # Parse line items from form
        line_idx = 0
        total = 0
        while True:
            pn = data.get(f'line_pn_{line_idx}')
            if pn is None:
                break
            qty = int(data.get(f'line_qty_{line_idx}', 0) or 0)
            unit_cost = float(data.get(f'line_cost_{line_idx}', 0) or 0)
            if pn and qty > 0 and unit_cost > 0:
                comp = Component.query.filter_by(part_number=pn).first()
                if comp:
                    line = InvoiceLine(
                        invoice_id=inv.id, component_id=comp.id,
                        qty=qty, unit_cost=unit_cost
                    )
                    db.session.add(line)
                    total += qty * unit_cost
                    # Update component's weighted average cost
                    if comp.unit_cost and comp.qty > 0:
                        # Weighted average: (old_cost * old_qty + new_cost * new_qty) / (old_qty + new_qty)
                        old_total = comp.unit_cost * comp.qty
                        comp.unit_cost = round((old_total + unit_cost * qty) / (comp.qty + qty), 4)
                    else:
                        comp.unit_cost = unit_cost
            line_idx += 1

        inv.total_amount = round(total, 2)
        db.session.commit()
        flash(f'Invoice {invoice_number} created — {line_idx} lines, ${total:,.2f} total', 'success')
        return redirect(url_for('invoices_page'))

    @app.route('/api/invoice/<int:inv_id>/file')
    @login_required
    def download_invoice_file(inv_id):
        import base64
        inv = Invoice.query.get_or_404(inv_id)
        if not inv.file_data:
            flash('No file attached', 'error')
            return redirect(url_for('invoices_page'))
        data = base64.b64decode(inv.file_data)
        mime = 'application/pdf' if inv.file_name.lower().endswith('.pdf') else 'application/octet-stream'
        return Response(data, mimetype=mime,
            headers={'Content-Disposition': f'inline; filename="{inv.file_name}"'})

    @app.route('/api/invoice/<int:inv_id>', methods=['DELETE'])
    @login_required
    def delete_invoice(inv_id):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        inv = Invoice.query.get_or_404(inv_id)
        db.session.delete(inv)
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/snapshot/generate', methods=['POST'])
    @login_required
    def generate_snapshot():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        email_to = request.json.get('email_to', 'sean@askwold.com,info@ecopowerparts.com')
        result = generate_inventory_snapshot(email_to=email_to)
        return jsonify(result)

    @app.route('/api/component/cost', methods=['POST'])
    @login_required
    def update_component_cost():
        """Manually update a component's unit cost."""
        data = request.get_json()
        pn = data.get('part_number')
        cost = float(data.get('unit_cost', 0))
        comp = Component.query.filter_by(part_number=pn).first()
        if not comp:
            return jsonify({'error': 'Component not found'}), 404
        comp.unit_cost = cost
        db.session.commit()
        return jsonify({'ok': True, 'unit_cost': comp.unit_cost})

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

    # ── Update Kit Prices (one-time migration) ──────────────────

    @app.route('/api/update-prices', methods=['POST'])
    @login_required
    def update_prices():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        from seed_data import KITS
        updated = []
        for slug, kit_info in KITS.items():
            kit = Kit.query.filter_by(slug=slug).first()
            if kit and kit_info.get('retail_price'):
                kit.retail_price = kit_info['retail_price']
                updated.append(f"{kit.name}: ${kit.retail_price}")
        db.session.commit()
        return jsonify({'ok': True, 'updated': updated})

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
