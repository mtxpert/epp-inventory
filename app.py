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
                     Supplier, SupplierComponent, PurchaseOrder, PurchaseOrderLine)

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
        # Add retail_price column if missing
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(db.engine)
        kit_cols = [c['name'] for c in inspector.get_columns('kits')]
        if 'retail_price' not in kit_cols:
            db.session.execute(db.text("ALTER TABLE kits ADD COLUMN retail_price FLOAT DEFAULT 0"))
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
                 'notes': 'All pipes', 'category': 'pipes'},
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
                    for comp in Component.query.filter_by(category=sd['category']).all():
                        db.session.add(SupplierComponent(supplier_id=s.id, component_id=comp.id))
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
            return jsonify({'ok': True})
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
