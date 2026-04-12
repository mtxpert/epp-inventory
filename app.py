#!/usr/bin/env python3
"""EPP Inventory Manager — Cloud-deployed inventory & BOM system."""
import os
import io
import csv
import json
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, Response, current_app
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail
from flask_apscheduler import APScheduler
from models import (db, User, Component, Kit, KitComponent, InventoryLog, ShopifyOrder,
                     Supplier, SupplierComponent, PurchaseOrder, PurchaseOrderLine,
                     InventorySnapshot, Invoice, InvoiceLine, Turn14OrderLog,
                     DealerInventory, DealerOrder, ReorderApproval)

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
    app.config['MOUSER_API_KEY'] = os.environ.get('MOUSER_API_KEY', '')
    app.config['TURN14_CLIENT_ID'] = os.environ.get('TURN14_CLIENT_ID', '')
    app.config['TURN14_CLIENT_SECRET'] = os.environ.get('TURN14_CLIENT_SECRET', '')
    app.config['SHIPSTATION_API_KEY'] = os.environ.get('SHIPSTATION_API_KEY', '')
    app.config['SHIPSTATION_V1_KEY'] = os.environ.get('SHIPSTATION_V1_KEY', '')
    app.config['SHIPSTATION_V1_SECRET'] = os.environ.get('SHIPSTATION_V1_SECRET', '')

    # Mail config
    app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS'] = True
    app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
    app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'info@ecopowerparts.com')
    app.config['SENDGRID_API_KEY'] = os.environ.get('SENDGRID_API_KEY', '')
    app.config['PAYPAL_CLIENT_ID'] = os.environ.get('PAYPAL_CLIENT_ID', '')
    app.config['PAYPAL_CLIENT_SECRET'] = os.environ.get('PAYPAL_CLIENT_SECRET', '')
    # Silicone Intakes auto-order credentials
    app.config['SI_USERNAME'] = os.environ.get('SI_USERNAME', '')
    app.config['SI_PASSWORD'] = os.environ.get('SI_PASSWORD', '')
    app.config['SI_CC_NAME']   = os.environ.get('SI_CC_NAME', '')
    app.config['SI_CC_NUMBER'] = os.environ.get('SI_CC_NUMBER', '')
    app.config['SI_CC_CVV']    = os.environ.get('SI_CC_CVV', '')
    app.config['SI_CC_EXPIRY'] = os.environ.get('SI_CC_EXPIRY', '')   # MM/YY
    app.config['SI_CC_ZIP']    = os.environ.get('SI_CC_ZIP', '')
    app.config['SI_SHIP_FIRST']   = os.environ.get('SI_SHIP_FIRST', 'Joshua')
    app.config['SI_SHIP_LAST']    = os.environ.get('SI_SHIP_LAST', 'Durmaj')
    app.config['SI_SHIP_ADDRESS'] = os.environ.get('SI_SHIP_ADDRESS', '910 S Hohokam')
    app.config['SI_SHIP_ADDRESS2']= os.environ.get('SI_SHIP_ADDRESS2', '#118')
    app.config['SI_SHIP_CITY']    = os.environ.get('SI_SHIP_CITY', 'Tempe')
    app.config['SI_SHIP_STATE']   = os.environ.get('SI_SHIP_STATE', 'AZ')
    app.config['SI_SHIP_ZIP']     = os.environ.get('SI_SHIP_ZIP', '85281')

    # Scheduler config
    app.config['SCHEDULER_API_ENABLED'] = False
    app.config['JOBS'] = [
        {
            'id': 'sync_orders',
            'func': 'app:scheduled_sync',
            'trigger': 'interval',
            'hours': 6,
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
        },
        {
            'id': 'turn14_sync',
            'func': 'app:scheduled_turn14_sync',
            'trigger': 'interval',
            'hours': 1,
            'misfire_grace_time': 900
        },
        {
            'id': 't14_access_check',
            'func': 'app:scheduled_t14_access_check',
            'trigger': 'cron',
            'hour': 9,
            'minute': 7,
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
        # Add unit_cost column to components if missing (must be before any Component queries)
        comp_cols = [c['name'] for c in inspector.get_columns('components')]
        if 'unit_cost' not in comp_cols:
            db.session.execute(db.text("ALTER TABLE components ADD COLUMN unit_cost FLOAT DEFAULT 0"))
            db.session.commit()
        # Add must_change_password column to users if missing
        user_cols = [c['name'] for c in inspector.get_columns('users')]
        if 'must_change_password' not in user_cols:
            db.session.execute(db.text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT FALSE"))
            db.session.commit()
        # Add label_url column to dealer_orders if missing
        if db.inspect(db.engine).has_table('dealer_orders'):
            do_cols = [c['name'] for c in inspector.get_columns('dealer_orders')]
            if 'label_url' not in do_cols:
                db.session.execute(db.text("ALTER TABLE dealer_orders ADD COLUMN label_url VARCHAR(500) DEFAULT ''"))
                db.session.commit()
        # Add notes column to kits if missing
        if 'notes' not in kit_cols:
            db.session.execute(db.text("ALTER TABLE kits ADD COLUMN notes VARCHAR(200) DEFAULT ''"))
            db.session.commit()
        # Add moq column to supplier_components if missing
        sc_cols = [c['name'] for c in inspector.get_columns('supplier_components')]
        if 'moq' not in sc_cols:
            db.session.execute(db.text("ALTER TABLE supplier_components ADD COLUMN moq INTEGER DEFAULT 0"))
            db.session.commit()
        # Add po_id column to reorder_approvals if table already exists without it
        if inspector.has_table('reorder_approvals'):
            ra_cols = [c['name'] for c in inspector.get_columns('reorder_approvals')]
            if 'po_id' not in ra_cols:
                db.session.execute(db.text("ALTER TABLE reorder_approvals ADD COLUMN po_id INTEGER REFERENCES purchase_orders(id)"))
                db.session.commit()
        # Mark raptor kits as delayed
        for rslug in ['raptor_sw_harness', 'raptor_console_harness']:
            rkit = Kit.query.filter_by(slug=rslug).first()
            if rkit and rkit.notes != 'Delayed — waiting for new harnesses':
                rkit.notes = 'Delayed — waiting for new harnesses'
        db.session.commit()
        from seed_data import seed_database
        seed_database()
        # Update kit prices from seed data
        from seed_data import KITS
        for slug, kit_info in KITS.items():
            kit = Kit.query.filter_by(slug=slug).first()
            if kit and kit_info.get('retail_price') and kit.retail_price != kit_info['retail_price']:
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
                {'name': 'Kevin Wolfe / Powill', 'email': 'kwolfe@powill.com', 'contact_name': 'Kevin',
                 'notes': 'BOV mounts and MAP sensor mounts', 'parts': ['BOV-SHO', 'BOV-FUSION', 'MAP-SHO']},
                {'name': 'Silicone Intakes', 'email': 'orders@siliconeintakes.com', 'contact_name': '',
                 'notes': 'T-bolt clamps — online ordering at siliconeintakes.com',
                 'parts': ['CLAMP-150', 'CLAMP-175', 'CLAMP-200', 'CLAMP-250', 'CLAMP-275', 'CLAMP-300']},
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
        # Add MAP-SHO component if missing
        if not Component.query.filter_by(part_number='MAP-SHO').first():
            map_comp = Component(part_number='MAP-SHO', name='SHO MAP Sensor Mount',
                                 category='misc', qty=0, reorder_threshold=10)
            db.session.add(map_comp)
            db.session.commit()
        # Add Kevin Wolfe supplier if missing
        if not Supplier.query.filter_by(name='Kevin Wolfe / Powill').first():
            kw = Supplier(name='Kevin Wolfe / Powill', email='kwolfe@powill.com',
                          contact_name='Kevin', notes='BOV mounts and MAP sensor mounts')
            db.session.add(kw)
            db.session.flush()
            for pn in ['BOV-SHO', 'BOV-FUSION', 'MAP-SHO']:
                comp = Component.query.filter_by(part_number=pn).first()
                if comp:
                    db.session.add(SupplierComponent(supplier_id=kw.id, component_id=comp.id))
            db.session.commit()
        # Add Raptor steering wheel components if missing
        if not Component.query.filter_by(part_number='RAPT-CON-LSW').first():
            raptor_parts = {
                'RAPT-CON-LSW': 'Left Switch Connector (34824-0124)',
                'RAPT-CON-RSW': 'Right Switch Connector (34824-0125)',
                'RAPT-CON-CSM': 'Clock Spring Male Connector (30968-1167)',
                'RAPT-CON-SHM': 'Shifter Male Connector (30968-1127)',
                'RAPT-CON-CSF': 'Clock Spring Female Connector (30700-1167)',
                'RAPT-CON-SHF': 'Shifter Female Connector (30700-1120)',
                'RAPT-CON-SCCM': 'SCCM Female Connector (7287-2043-30)',
                'RAPT-CON-PSB': 'Paddle Shifter Black Connector (2138557-2)',
                'RAPT-CON-PSG': 'Paddle Shifter Grey Connector (2138557-1)',
                'RAPT-CON-HORN': 'Horn Connector (12059252)',
                'RAPT-PIN-LSW': 'Left Switch Pins x12/kit (560023-0421)',
                'RAPT-PIN-CSM': 'Clock Spring Male Pins x10/kit (TE 2-1419158-5)',
                'RAPT-PIN-CSF': 'Clock Spring Female Pins x24/kit (TE 1393366-1)',
                'RAPT-PIN-SCCM': 'SCCM Female Pins x3/kit (TE 2035334-2)',
                'RAPT-PIN-PS': 'Paddle Shifter Pins x4/kit (2098762-1)',
                'RAPT-PIN-HORN': 'Horn Pins x2/kit (12059894-L)',
                'RAPT-PCB-L': 'Raptor Steering Wheel PCB — Left',
                'RAPT-PCB-R': 'Raptor Steering Wheel PCB — Right',
            }
            for pn, name in raptor_parts.items():
                db.session.add(Component(part_number=pn, name=name, category='raptor', qty=0, reorder_threshold=10))
            db.session.commit()
        # Set PCB stock to 70 if still at 0 (initial inventory count)
        for pcb_pn in ['RAPT-PCB-L', 'RAPT-PCB-R']:
            pcb = Component.query.filter_by(part_number=pcb_pn).first()
            if pcb and pcb.qty == 0:
                pcb.qty = 70
        db.session.commit()
        # Add finished-assembly components for raptor harnesses (tracks assembled units on hand)
        for pn, name, init_qty in [
            ('RAPT-ASSY-SW',  'Raptor SW Harness — Assembled',      0),
            ('RAPT-ASSY-CON', 'Raptor Console Harness — Assembled', 0),
        ]:
            if not Component.query.filter_by(part_number=pn).first():
                db.session.add(Component(part_number=pn, name=name, category='raptor',
                                         qty=init_qty, reorder_threshold=5))
        db.session.commit()
        # Set real on-hand quantities for assembled harnesses (only updates once if still at 0)
        sw_assy = Component.query.filter_by(part_number='RAPT-ASSY-SW').first()
        con_assy = Component.query.filter_by(part_number='RAPT-ASSY-CON').first()
        if sw_assy and sw_assy.qty == 0:
            sw_assy.qty = -1   # sold one, waiting on restock (20 on order)
        if con_assy and con_assy.qty == 0:
            con_assy.qty = 3
        db.session.commit()
        # Add Raptor kits if missing — BOMs now reference assembled units
        for rslug, rname, assy_pn in [
            ('raptor_sw_harness',      'Raptor Steering Wheel Harness',   'RAPT-ASSY-SW'),
            ('raptor_console_harness', 'Raptor Console Shifter Harness',   'RAPT-ASSY-CON'),
        ]:
            rk = Kit.query.filter_by(slug=rslug).first()
            if not rk:
                rk = Kit(slug=rslug, name=rname, retail_price=360 if 'sw' in rslug else 0)
                db.session.add(rk)
                db.session.flush()
            # Ensure BOM is the assembled harness component (replace raw-parts BOM if still there)
            assy_comp = Component.query.filter_by(part_number=assy_pn).first()
            if assy_comp:
                already = KitComponent.query.filter_by(kit_id=rk.id, component_id=assy_comp.id).first()
                if not already:
                    # Clear old raw-component BOM, replace with single assembled-unit entry
                    KitComponent.query.filter_by(kit_id=rk.id).delete()
                    db.session.add(KitComponent(kit_id=rk.id, component_id=assy_comp.id, quantity=1))
        db.session.commit()
        # Add Mouser supplier if missing
        if not Supplier.query.filter_by(name='Mouser Electronics').first():
            mouser = Supplier(name='Mouser Electronics', email='', contact_name='',
                              notes='Raptor connector housings and pin terminals')
            db.session.add(mouser)
            db.session.flush()
            for comp in Component.query.filter(Component.part_number.like('RAPT-CON-%') | Component.part_number.like('RAPT-PIN-%')).all():
                db.session.add(SupplierComponent(supplier_id=mouser.id, component_id=comp.id))
            db.session.commit()
        # Add Sean / Innova Speed supplier if missing
        if not Supplier.query.filter_by(name='Innova Speed').first():
            innova = Supplier(name='Innova Speed', email='sean@innovaspeed.com', contact_name='Sean',
                              notes='Assembles Raptor steering wheel harnesses from Mouser parts')
            db.session.add(innova)
            db.session.commit()
        # Add Jason / Cybernetworks supplier if missing
        if not Supplier.query.filter_by(name='Cybernetworks LLC').first():
            cyber = Supplier(name='Cybernetworks LLC', email='jason@cybernetworksllc.com', contact_name='Jason',
                             notes='Raptor steering wheel circuit boards (L and R)')
            db.session.add(cyber)
            db.session.flush()
            for comp in Component.query.filter(Component.part_number.like('RAPT-PCB-%')).all():
                db.session.add(SupplierComponent(supplier_id=cyber.id, component_id=comp.id))
            db.session.commit()
        # Create default admin if no users exist
        if not User.query.first():
            admin = User(email='info@ecopowerparts.com', name='Mike', role='admin')
            admin.set_password(os.environ.get('ADMIN_PASSWORD', 'changeme123'))
            db.session.add(admin)
            db.session.commit()

    scheduler.init_app(app)
    scheduler.start()

    import json as _json_mod
    app.jinja_env.filters['from_json'] = lambda s: _json_mod.loads(s) if s else []

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
    """Daily 8AM stock check — sends approval email for items with suppliers, plain alert otherwise."""
    from shopify_sync import get_low_stock_components, send_reorder_approval_email, send_low_stock_alert
    with app.app_context():
        low = get_low_stock_components()
        if low:
            try:
                send_reorder_approval_email(low)
            except Exception as e:
                app.logger.error(f"Approval email failed, falling back: {e}")
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
    # Raptor kits use PCBs as limiting factor instead of pipes
    pipe_stock = {c.id: c.qty for c in components if c.category == 'pipes'}
    pcb_stock = {c.id: c.qty for c in components if c.part_number.startswith('RAPT-PCB-')}
    kit_priority = {
        'hot_pipes_sho': 1, 'intake_stock_hose': 2, 'intake_custom_hose': 3,
        'hot_pipes_explorer': 4, 'fusion_intake': 5, 'fusion_charge': 6,
        'f150_intake': 7, 'nmd_upgrade': 8, 'nmd': 9, 'explorer_nmd': 10,
        'raptor_sw_harness': 11, 'raptor_console_harness': 12,
    }
    kits_by_priority = sorted(kits, key=lambda k: kit_priority.get(k.slug, 99))
    kit_retail_details = []
    for kit in kits_by_priority:
        pipe_parts = [kc for kc in kit.components if kc.component.category == 'pipes']
        pcb_parts = [kc for kc in kit.components if kc.component.part_number.startswith('RAPT-PCB-')]
        if pcb_parts:
            # Raptor kits: limit by PCB (circuit board) stock
            max_build = min(pcb_stock.get(kc.component_id, 0) // kc.quantity for kc in pcb_parts)
            for kc in pcb_parts:
                pcb_stock[kc.component_id] = pcb_stock.get(kc.component_id, 0) - (kc.quantity * max_build)
        elif pipe_parts:
            max_build = min(pipe_stock.get(kc.component_id, 0) // kc.quantity for kc in pipe_parts)
            for kc in pipe_parts:
                pipe_stock[kc.component_id] = pipe_stock.get(kc.component_id, 0) - (kc.quantity * max_build)
        else:
            continue
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
    body += f"Total Retail Value (buildable kits): ${total_retail:,.2f}\n"
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


def scheduled_turn14_sync():
    """Hourly — sync Turn14 inventory/pricing for lowering kit parts."""
    with app.app_context():
        try:
            from turn14_sync import sync_lowering_kit_inventory
            results = sync_lowering_kit_inventory()
            in_stock = [p for p, d in results.items() if d.get('in_stock')]
            app.logger.info(f"Turn14 sync complete. In stock: {in_stock}")
        except Exception as e:
            app.logger.error(f"Turn14 sync failed: {e}")


def scheduled_t14_access_check():
    """Daily 9:07am — warn if no Turn14 API order in 55+ days (access revoked at 60 days)."""
    with app.app_context():
        try:
            last = Turn14OrderLog.query.filter_by(environment='production').order_by(
                Turn14OrderLog.placed_at.desc()
            ).first()
            now = datetime.now(timezone.utc)
            if last is None:
                # No production orders yet — use the approval date as baseline
                baseline = datetime(2026, 3, 23, tzinfo=timezone.utc)
                days_since = (now - baseline).days
            else:
                placed_at = last.placed_at
                if placed_at.tzinfo is None:
                    placed_at = placed_at.replace(tzinfo=timezone.utc)
                days_since = (now - placed_at).days

            if days_since >= 60:
                # Email Mark Eder AND ourselves
                subject = "Turn14 API Access — Requesting Continued Access (EcoPowerParts)"
                body = (
                    f"Hi Mark,\n\n"
                    f"We're reaching out to maintain our Turn14 API integration for EcoPowerParts. "
                    f"It has been {days_since} days since our last production API order. "
                    f"We'd like to keep our access active — please let us know if any action "
                    f"is needed on our end.\n\n"
                    f"Thank you,\nMike Bambic\nEcoPowerParts\ninfo@ecopowerparts.com"
                )
                for recipient in ['apisupport@turn14.com', 'info@ecopowerparts.com']:
                    msg = Message(subject=subject, recipients=[recipient], body=body)
                    mail.send(msg)
                app.logger.warning(f"Turn14 access check: {days_since} days — emailed Mark Eder")
            elif days_since >= 55:
                # Internal warning only
                msg = Message(
                    subject=f"[EPP WARNING] Turn14 API access expires in {60 - days_since} days",
                    recipients=['info@ecopowerparts.com'],
                    body=(
                        f"Turn14 API access will be revoked in {60 - days_since} days "
                        f"unless a production order is placed.\n\n"
                        f"Last production order: {last.po_number if last else 'never'}\n"
                        f"Days since last order: {days_since}\n\n"
                        f"Place an order or contact Mark Eder at apisupport@turn14.com."
                    )
                )
                mail.send(msg)
                app.logger.warning(f"Turn14 access check: {days_since} days — internal warning sent")
            else:
                app.logger.info(f"Turn14 access check: {days_since} days since last order — OK")
        except Exception as e:
            app.logger.error(f"Turn14 access check failed: {e}")


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
                if user.must_change_password:
                    return redirect(url_for('force_change_password'))
                next_page = request.args.get('next')
                if not next_page and user.role == 'dealer':
                    return redirect(url_for('dealer_portal'))
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
        current_user.must_change_password = False
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/force-change-password', methods=['GET', 'POST'])
    @login_required
    def force_change_password():
        if not current_user.must_change_password:
            return redirect(url_for('dealer_portal') if current_user.role == 'dealer' else url_for('dashboard'))
        error = None
        if request.method == 'POST':
            new_pw = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')
            if len(new_pw) < 8:
                error = 'Password must be at least 8 characters.'
            elif new_pw != confirm:
                error = 'Passwords do not match.'
            else:
                current_user.set_password(new_pw)
                current_user.must_change_password = False
                db.session.commit()
                return redirect(url_for('dealer_portal') if current_user.role == 'dealer' else url_for('dashboard'))
        return render_template('force_change_password.html', error=error)

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
        user = User(email=email, name=name, role=role,
                    must_change_password=(role == 'dealer'))
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
        cat_order = ['pipes', 'couplers', 'clamps', 'misc', 'raptor']
        cat_labels = {'pipes': 'Pipes', 'couplers': 'Silicone Hoses & Couplers', 'clamps': 'Clamps', 'misc': 'Misc / Hardware', 'raptor': 'Raptor Steering Wheel'}
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

        recent_orders = ShopifyOrder.query.order_by(ShopifyOrder.id.desc()).limit(100).all()
        recent_logs = InventoryLog.query.order_by(InventoryLog.created_at.desc()).limit(50).all()

        # Calculate "on order" projection: pending POs → projected buildable kits → retail value
        pending_pos = PurchaseOrder.query.filter(PurchaseOrder.status.in_(['draft', 'sent'])).all()
        projected_stock = {c.id: c.qty for c in components}
        for po in pending_pos:
            for line in po.lines:
                projected_stock[line.component_id] = projected_stock.get(line.component_id, 0) + line.qty

        # Recalculate pipe/PCB-limited buildable with projected stock
        proj_pipe_stock = {cid: qty for cid, qty in projected_stock.items()
                          if any(c.id == cid and c.category == 'pipes' for c in components)}
        proj_pcb_stock = {cid: qty for cid, qty in projected_stock.items()
                         if any(c.id == cid and c.part_number.startswith('RAPT-PCB-') for c in components)}
        proj_kit_buildable = {}
        for kit in kits_by_priority:
            pipe_parts = [kc for kc in kit.components if kc.component.category == 'pipes']
            pcb_parts = [kc for kc in kit.components if kc.component.part_number.startswith('RAPT-PCB-')]
            if pcb_parts:
                # Raptor kits: limit by PCB (circuit board) stock
                max_build = min(proj_pcb_stock.get(kc.component_id, 0) // kc.quantity for kc in pcb_parts)
                proj_kit_buildable[kit.id] = max_build
                for kc in pcb_parts:
                    proj_pcb_stock[kc.component_id] = proj_pcb_stock.get(kc.component_id, 0) - (kc.quantity * max_build)
            elif pipe_parts:
                max_build = min(proj_pipe_stock.get(kc.component_id, 0) // kc.quantity for kc in pipe_parts)
                proj_kit_buildable[kit.id] = max_build
                for kc in pipe_parts:
                    proj_pipe_stock[kc.component_id] = proj_pipe_stock.get(kc.component_id, 0) - (kc.quantity * max_build)
            else:
                proj_kit_buildable[kit.id] = 0

        projected_retail_value = sum(
            proj_kit_buildable.get(kit.id, 0) * (kit.retail_price or 0)
            for kit in kits if proj_kit_buildable.get(kit.id, 0) > 0
        )
        on_order_value = projected_retail_value - total_retail_value

        # Build MoQ map for Silicone Intakes clamps {part_number: moq}
        si = Supplier.query.filter_by(name='Silicone Intakes').first()
        moq_map = {}
        if si:
            for sc in SupplierComponent.query.filter_by(supplier_id=si.id).all():
                if sc.moq:
                    moq_map[sc.component.part_number] = sc.moq

        return render_template('dashboard.html',
            components=components, kits=kits, categories=categories,
            low_stock=low_stock, total_stock=total_stock,
            kit_buildable=kit_buildable, min_buildable=min_buildable,
            kit_pipe_buildable=kit_pipe_buildable,
            total_retail_value=total_retail_value,
            projected_retail_value=projected_retail_value,
            on_order_value=on_order_value,
            pending_pos=pending_pos,
            proj_kit_buildable=proj_kit_buildable,
            used_in=used_in, recent_orders=recent_orders, recent_logs=recent_logs,
            moq_map=moq_map)

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

    @app.route('/api/moq', methods=['POST'])
    @login_required
    def set_moq():
        """Set MoQ for a component on its Silicone Intakes supplier link."""
        data = request.get_json()
        pn = data.get('part_number')
        moq = int(data.get('moq', 0))
        comp = Component.query.filter_by(part_number=pn).first()
        if not comp:
            return jsonify({'error': 'Component not found'}), 404
        si = Supplier.query.filter_by(name='Silicone Intakes').first()
        if not si:
            return jsonify({'error': 'Silicone Intakes supplier not found'}), 404
        sc = SupplierComponent.query.filter_by(supplier_id=si.id, component_id=comp.id).first()
        if not sc:
            # Create the supplier-component link on the fly
            sc = SupplierComponent(supplier_id=si.id, component_id=comp.id, unit_cost=0, moq=0)
            db.session.add(sc)
            db.session.flush()
        sc.moq = moq
        db.session.commit()
        return jsonify({'ok': True, 'part_number': pn, 'moq': sc.moq})

    @app.route('/api/sync-orders', methods=['POST'])
    @login_required
    def sync_orders():
        from shopify_sync import sync_recent_orders
        hours = int((request.get_json(silent=True) or {}).get('hours', 24))
        result = sync_recent_orders(hours=hours)
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

        retail_price = float(data.get('retail_price', 0) or 0)
        kit = Kit(slug=slug, name=name, shopify_id=shopify_id, shopify_variant=shopify_variant, retail_price=retail_price)
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
        if 'retail_price' in data:
            kit.retail_price = float(data['retail_price'] or 0)

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

    @app.route('/receiving')
    @login_required
    def receiving():
        open_pos = PurchaseOrder.query.filter(
            PurchaseOrder.status.in_(['draft', 'sent'])
        ).order_by(PurchaseOrder.created_at.desc()).all()
        return render_template('receiving.html', open_pos=open_pos)

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

    def _smtp_send(to_addr, subject, body):
        """Send email via Gmail SMTP."""
        import smtplib
        from email.mime.text import MIMEText
        username = current_app.config.get('MAIL_USERNAME', '')
        password = current_app.config.get('MAIL_PASSWORD', '')
        sender = current_app.config.get('MAIL_DEFAULT_SENDER', username)
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = to_addr
        with smtplib.SMTP('smtp.forwardemail.net', 2525, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.login(username, password)
            s.sendmail(sender, [to_addr], msg.as_string())

    @app.route('/api/po/<int:po_id>/test', methods=['POST'])
    @login_required
    def test_po(po_id):
        try:
            po = PurchaseOrder.query.get(po_id)
            if not po:
                return jsonify({'error': 'not found'}), 404
            return jsonify({'ok': True, 'po': po.po_number, 'supplier': po.supplier.name, 'email': po.supplier.email})
        except BaseException as e:
            import traceback
            return jsonify({'error': str(e), 'type': type(e).__name__, 'traceback': traceback.format_exc()}), 500

    @app.route('/api/email-test', methods=['POST'])
    @login_required
    def email_test():
        """Diagnostic: test _smtp_send directly."""
        try:
            to = request.json.get('to', 'mbambic@gmail.com') if request.is_json else 'mbambic@gmail.com'
            _smtp_send(to, '[EPP] Email test', 'This is a test email from EPP inventory.')
            return jsonify({'ok': True, 'sent_to': to})
        except Exception as e:
            import traceback
            return jsonify({'error': str(e), 'type': type(e).__name__,
                            'traceback': traceback.format_exc()}), 200

    @app.route('/api/po/<int:po_id>/build', methods=['POST'])
    @login_required
    def build_po_body(po_id):
        """Diagnostic: build PO body and return it without sending email."""
        try:
            po = PurchaseOrder.query.get(po_id)
            if not po:
                return jsonify({'error': 'not found'}), 404
            step = 'start'
            date_str = po.created_at.strftime('%B %d, %Y') if po.created_at else 'N/A'
            step = 'date_ok'
            subtotal = sum(l.qty * (l.unit_cost or 0) for l in po.lines)
            step = 'subtotal_ok'
            lines_info = [{'part': l.component.part_number if l.component else 'NONE',
                           'name': l.component.name if l.component else 'NONE',
                           'qty': l.qty, 'cost': str(l.unit_cost)} for l in po.lines]
            step = 'lines_ok'
            body = ''
            for line in po.lines:
                unit = f"${line.unit_cost:.2f}" if line.unit_cost else "TBD"
                total = f"${line.qty * line.unit_cost:.2f}" if line.unit_cost else "TBD"
                body += f"{line.component.part_number:<15} {line.component.name:<32} {line.qty:>5} {unit:>8} {total:>9}\n"
            step = 'format_ok'
            return jsonify({'ok': True, 'step': step, 'lines': lines_info, 'body_preview': body})
        except Exception as e:
            import traceback
            return jsonify({'error': str(e), 'step': step, 'type': type(e).__name__,
                            'traceback': traceback.format_exc()}), 500

    @app.route('/api/po/<int:po_id>/send', methods=['POST'])
    @login_required
    def send_po(po_id):
        try:
            po = PurchaseOrder.query.get(po_id)
            if not po:
                return jsonify({'error': f'PO {po_id} not found'}), 404
            date_str = po.created_at.strftime('%B %d, %Y') if po.created_at else 'N/A'
            subtotal = sum(l.qty * (l.unit_cost or 0) for l in po.lines)

            body  = "=" * 65 + "\n"
            body += "                     PURCHASE ORDER\n"
            body += "=" * 65 + "\n\n"
            body += "Eco Power Parts\n"
            body += "910 S Hohokam Dr #118\n"
            body += "Tempe, AZ 85281\n"
            body += "Phone: (602) 505-0701\n"
            body += "info@ecopowerparts.com\n\n"
            body += f"PO #:  {po.po_number}\n"
            body += f"Date:  {date_str}\n\n"
            body += "VENDOR:\n"
            body += f"  {po.supplier.name}\n"
            if po.supplier.email:
                body += f"  {po.supplier.email}\n"
            body += "\n"
            body += "SHIP TO:\n"
            body += "  Eco Power Parts\n"
            body += "  910 S Hohokam Dr #118\n"
            body += "  Tempe, AZ 85281\n"
            body += "  (602) 505-0701\n\n"
            body += "-" * 65 + "\n"
            body += f"{'ITEM':<15} {'DESCRIPTION':<32} {'QTY':>5} {'UNIT':>8} {'TOTAL':>9}\n"
            body += "-" * 65 + "\n"
            for line in po.lines:
                unit = f"${line.unit_cost:.2f}" if line.unit_cost else "TBD"
                total = f"${line.qty * line.unit_cost:.2f}" if line.unit_cost else "TBD"
                body += f"{line.component.part_number:<15} {line.component.name:<32} {line.qty:>5} {unit:>8} {total:>9}\n"
            body += "-" * 65 + "\n"
            if subtotal:
                body += f"{'':>54} SUBTOTAL: ${subtotal:>8.2f}\n"
                body += f"{'':>54} SHIPPING:      TBD\n"
            body += "\n"
            if po.notes:
                body += f"Notes: {po.notes}\n\n"
            body += "Please confirm receipt and estimated ship date.\n"
            body += "Thank you,\nMike Bambic\nEco Power Parts\n"

            if not po.supplier.email:
                return jsonify({'error': f'Supplier {po.supplier.name} has no email address'}), 400
            current_app.logger.info(f"send_po: calling _smtp_send to {po.supplier.email}")
            _smtp_send(po.supplier.email, f"[EPP] Purchase Order {po.po_number}", body)
            current_app.logger.info(f"send_po: email sent, updating status")
            po.status = 'sent'
            po.sent_at = datetime.now(timezone.utc)
            db.session.commit()

            # Auto-companion: if PO contains HP-NMD, send Kevin Wolfe a MAP-SHO PO
            companion_result = None
            nmd_qty = sum(l.qty for l in po.lines if l.component.part_number == 'HP-NMD')
            if nmd_qty > 0:
                companion_result = _send_map_mount_po(nmd_qty, po.po_number)

            return jsonify({'ok': True, 'email_body': body, 'companion_po': companion_result})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            current_app.logger.error(f"send_po error: {e}\n{tb}")
            return jsonify({'error': str(e), 'traceback': tb}), 500
        except BaseException as e:
            import traceback
            tb = traceback.format_exc()
            current_app.logger.error(f"send_po BaseException: {e}\n{tb}")
            return jsonify({'error': str(e), 'traceback': tb, 'type': type(e).__name__}), 500

    def _send_map_mount_po(nmd_qty, parent_po_number):
        """Auto-create and email a Kevin Wolfe / Powill PO for MAP-SHO mounts.
        Triggered whenever an NMD PO is sent — Kevin ships direct to Fontana for assembly."""
        try:
            kw = Supplier.query.filter_by(name='Kevin Wolfe / Powill').first()
            map_comp = Component.query.filter_by(part_number='MAP-SHO').first()
            if not kw or not map_comp:
                return {'error': 'Kevin Wolfe supplier or MAP-SHO component not found'}

            count = PurchaseOrder.query.count() + 1
            po_number = f"EPP-PO-{count:04d}"
            po = PurchaseOrder(po_number=po_number, supplier_id=kw.id,
                               notes=f"Companion to {parent_po_number} — ship direct to Fontana")
            db.session.add(po)
            db.session.flush()
            line = PurchaseOrderLine(po_id=po.id, component_id=map_comp.id,
                                     qty=nmd_qty, unit_cost=0)
            db.session.add(line)

            date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')
            body  = "=" * 65 + "\n"
            body += "                     PURCHASE ORDER\n"
            body += "=" * 65 + "\n\n"
            body += "Eco Power Parts\n"
            body += "910 S Hohokam Dr #118\n"
            body += "Tempe, AZ 85281\n"
            body += "Phone: (602) 505-0701\n"
            body += "info@ecopowerparts.com\n\n"
            body += f"PO #:  {po_number}\n"
            body += f"Date:  {date_str}\n"
            body += f"Ref:   Companion to {parent_po_number}\n\n"
            body += "VENDOR:\n"
            body += "  Kevin Wolfe / Powill\n"
            body += "  kwolfe@powill.com\n\n"
            body += "SHIP TO:\n"
            body += "  11027 Jasmine Street\n"
            body += "  Fontana, California 92337\n\n"
            body += "-" * 65 + "\n"
            body += f"{'ITEM':<15} {'DESCRIPTION':<32} {'QTY':>5} {'UNIT':>8}\n"
            body += "-" * 65 + "\n"
            body += f"{'MAP-SHO':<15} {'SHO MAP Sensor Mount':<32} {nmd_qty:>5} {'TBD':>8}\n"
            body += "-" * 65 + "\n\n"
            body += f"Please ship direct to the Fontana address above.\n"
            body += f"This order accompanies NMD pipe PO {parent_po_number}.\n\n"
            body += "Please confirm receipt and estimated ship date.\n"
            body += "Thank you,\nMike Bambic\nEco Power Parts\n(602) 505-0701\n"

            _smtp_send(kw.email, f"[EPP] Purchase Order {po_number} — MAP Sensor Mounts x{nmd_qty}", body)
            po.status = 'sent'
            po.sent_at = datetime.now(timezone.utc)
            db.session.commit()
            return {'ok': True, 'po_number': po_number, 'qty': nmd_qty}
        except Exception as e:
            current_app.logger.error(f"MAP mount companion PO error: {e}")
            db.session.rollback()
            return {'error': str(e)}

    @app.route('/api/po/<int:po_id>/receive', methods=['POST'])
    @login_required
    def receive_po(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        data = request.get_json() or {}
        # per-line quantities: {"lines": [{"line_id": N, "qty": X}, ...]}
        line_qtys = {item['line_id']: int(item['qty']) for item in data.get('lines', [])} if data.get('lines') else None
        total_received = 0
        for line in po.lines:
            qty = line_qtys[line.id] if line_qtys and line.id in line_qtys else line.qty
            if qty <= 0:
                continue
            line.component.qty += qty
            total_received += qty
            log = InventoryLog(
                component_id=line.component_id, qty_change=qty,
                reason=f"PO {po.po_number} received", user_id=current_user.id
            )
            db.session.add(log)
        po.status = 'received'
        po.received_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({'ok': True, 'total_received': total_received})

    @app.route('/api/po/<int:po_id>/cancel', methods=['POST'])
    @login_required
    def cancel_po(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        po.status = 'cancelled'
        db.session.commit()
        return jsonify({'ok': True})

    # ── Reorder Approval Flow ─────────────────────────────────────────────────

    @app.route('/reorder/approve/<token>', methods=['GET'])
    def reorder_approve_page(token):
        """Show approval confirmation page (no login required — token is the auth)."""
        import secrets as _sec
        ra = ReorderApproval.query.filter_by(token=token).first_or_404()
        now = datetime.now(timezone.utc)
        if ra.status != 'pending':
            return render_template('reorder_approval.html', ra=ra, expired=False, already_acted=True)
        if ra.expires_at.replace(tzinfo=timezone.utc) < now:
            ra.status = 'expired'
            db.session.commit()
            return render_template('reorder_approval.html', ra=ra, expired=True, already_acted=False)
        items = json.loads(ra.items_json)
        return render_template('reorder_approval.html', ra=ra, items=items, expired=False, already_acted=False)

    @app.route('/reorder/approve/<token>/confirm', methods=['POST'])
    def reorder_approve_confirm(token):
        """Create the PO and place the order after approval."""
        ra = ReorderApproval.query.filter_by(token=token, status='pending').first_or_404()
        now = datetime.now(timezone.utc)
        if ra.expires_at.replace(tzinfo=timezone.utc) < now:
            ra.status = 'expired'
            db.session.commit()
            return render_template('reorder_approval.html', ra=ra, expired=True, already_acted=False)

        items = json.loads(ra.items_json)
        # Group by supplier
        by_supplier = {}
        for item in items:
            sid = item['supplier_id']
            by_supplier.setdefault(sid, []).append(item)

        created_pos = []
        for supplier_id, lines in by_supplier.items():
            supplier = Supplier.query.get(supplier_id)
            if not supplier:
                continue
            # Generate PO number
            last_po = PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).first()
            next_num = (last_po.id + 1) if last_po else 1
            po_number = f"EPP-PO-{next_num:04d}"
            # Avoid collision
            while PurchaseOrder.query.filter_by(po_number=po_number).first():
                next_num += 1
                po_number = f"EPP-PO-{next_num:04d}"
            po = PurchaseOrder(
                po_number=po_number, supplier_id=supplier_id,
                status='draft', notes='Auto-reorder — approved via email link',
                created_by=None
            )
            db.session.add(po)
            db.session.flush()
            for item in lines:
                comp = Component.query.filter_by(part_number=item['part_number']).first()
                if comp:
                    pol = PurchaseOrderLine(po_id=po.id, component_id=comp.id,
                                           qty=item['qty'], unit_cost=item.get('unit_cost', 0))
                    db.session.add(pol)
            created_pos.append(po)

        ra.status = 'approved'
        ra.acted_at = now
        if created_pos:
            ra.po_id = created_pos[0].id
        db.session.commit()

        # Place orders — Silicone Intakes gets auto-checkout, others get PO email
        po_results = []
        for po in created_pos:
            db.session.refresh(po)
            supplier = po.supplier
            if supplier.name == 'Silicone Intakes':
                try:
                    from silicone_checkout import place_clamp_order, load_cart
                    order_lines = [{'part_number': l.component.part_number,
                                    'qty': l.qty, 'unit_cost': l.unit_cost}
                                   for l in po.lines]
                    result = place_clamp_order(order_lines)
                    if result['ok']:
                        po.status = 'sent'
                        po.sent_at = now
                        po.notes = (po.notes or '') + f" | SI order #{result.get('order_number','')}"
                        po_results.append({
                            'po_number': po.po_number,
                            'status': 'ordered',
                            'detail': f"Order placed on siliconeintakes.com — ref #{result.get('order_number', 'confirmed')}"
                        })
                    else:
                        # Full order failed — fall back to cart-only so user can complete manually
                        current_app.logger.warning(f"SI auto-order failed ({result['error']}), falling back to cart load")
                        cart = load_cart(order_lines)
                        if cart['ok']:
                            po.status = 'sent'
                            po.sent_at = now
                            po_results.append({
                                'po_number': po.po_number,
                                'status': 'cart_ready',
                                'detail': f"Auto-order failed ({result['error']}) — cart loaded, click to complete checkout",
                                'checkout_url': cart['checkout_url']
                            })
                        else:
                            po_results.append({'po_number': po.po_number, 'status': 'error',
                                               'detail': result['error']})
                except Exception as e:
                    current_app.logger.error(f"SI order exception for {po.po_number}: {e}")
                    po_results.append({'po_number': po.po_number, 'status': 'error', 'detail': str(e)})
            else:
                # Email PO to supplier
                try:
                    from shopify_sync import _smtp_send
                    lines_text = "\n".join(
                        f"  {l.component.part_number} — {l.component.name} × {l.qty}  @ ${l.unit_cost:.2f}"
                        for l in po.lines
                    )
                    body = (f"Purchase Order: {po.po_number}\n"
                            f"From: EcoPowerParts\n\n"
                            f"Items:\n{lines_text}\n\n"
                            f"Total: ${po.total:.2f}\n\nAuto-generated reorder.\n")
                    if supplier.email:
                        _smtp_send([supplier.email], f"PO {po.po_number} — EcoPowerParts", body)
                    po.status = 'sent'
                    po.sent_at = now
                    po_results.append({'po_number': po.po_number, 'status': 'emailed',
                                       'detail': f"PO emailed to {supplier.name}"})
                except Exception as e:
                    current_app.logger.error(f"Failed to email PO {po.po_number}: {e}")
                    po_results.append({'po_number': po.po_number, 'status': 'error', 'detail': str(e)})
        db.session.commit()

        return render_template('reorder_approval.html', ra=ra, items=items,
                               approved=True, po_results=po_results,
                               expired=False, already_acted=False)

    @app.route('/reorder/approve/<token>/deny', methods=['POST'])
    def reorder_approve_deny(token):
        """Deny/dismiss the reorder."""
        ra = ReorderApproval.query.filter_by(token=token, status='pending').first_or_404()
        ra.status = 'denied'
        ra.acted_at = datetime.now(timezone.utc)
        db.session.commit()
        return render_template('reorder_approval.html', ra=ra, denied=True,
                               expired=False, already_acted=False)

    # ── End Reorder Approval Flow ──────────────────────────────────────────────

    # siliconeintakes.com product ID map — keyed by part_number
    SI_PRODUCT_IDS = {
        'CLAMP-150': 106,   # 1.5"
        'CLAMP-175': 107,   # 1.75"
        'CLAMP-200': 100,   # 2.0"
        'CLAMP-250': 102,   # 2.5"
        'CLAMP-275': 103,   # 2.75"
        'CLAMP-300': 104,   # 3.0"
    }

    @app.route('/api/po/<int:po_id>/place-online', methods=['POST'])
    @login_required
    def place_po_online(po_id):
        """Log in to siliconeintakes.com, add clamp line items to cart, return checkout URL."""
        import requests as req
        po = PurchaseOrder.query.get_or_404(po_id)

        si_user = current_app.config.get('SI_USERNAME', '')
        si_pass = current_app.config.get('SI_PASSWORD', '')
        if not si_user or not si_pass:
            return jsonify({'error': 'SI_USERNAME / SI_PASSWORD not configured'}), 500

        # Check all lines are mappable before touching the cart
        unmapped = [l.component.part_number for l in po.lines
                    if l.component.part_number not in SI_PRODUCT_IDS]
        if unmapped:
            return jsonify({'error': f'No siliconeintakes.com product ID for: {", ".join(unmapped)}'}), 400

        session = req.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; EPP-AutoOrder/1.0)'})

        # Login
        login_r = session.post(
            'https://www.siliconeintakes.com/account.php',
            data={'login_email_address': si_user, 'login_password': si_pass, 'action': 'process'},
            allow_redirects=True, timeout=15
        )
        if 'logout' not in login_r.text.lower() and 'my account' not in login_r.text.lower():
            return jsonify({'error': 'siliconeintakes.com login failed — check credentials'}), 500

        # Add each clamp line to cart
        added = []
        for line in po.lines:
            pid = SI_PRODUCT_IDS[line.component.part_number]
            r = session.post(
                'https://www.siliconeintakes.com/shopping_cart.php?action=add_product',
                data={'products_id': pid, 'cart_quantity': line.qty},
                allow_redirects=True, timeout=15
            )
            if r.status_code == 200:
                added.append({'part': line.component.part_number, 'qty': line.qty, 'product_id': pid})
            else:
                current_app.logger.warning(f"Cart add failed for {line.component.part_number}: {r.status_code}")

        # Extract session cookie to hand back to the browser
        osCsid = session.cookies.get('osCsid', '')
        cart_url = f'https://www.siliconeintakes.com/shopping_cart.php?osCsid={osCsid}'

        return jsonify({'ok': True, 'added': added, 'cart_url': cart_url, 'session_id': osCsid})

    @app.route('/api/po/rfq', methods=['POST'])
    @login_required
    def send_rfq():
        """Send RFQ email to supplier requesting pricing at multiple qty breaks."""
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
            _smtp_send(supplier.email, f"[EPP] Request for Quote — {len(parts)} parts", body)
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

    @app.route('/admin/repair-inventory', methods=['POST'])
    @login_required
    def repair_inventory_deductions():
        """One-time fix: reverse deductions for orders #1372-#1377 which were incorrectly
        processed — seed inventory already reflected stock after those orders shipped."""
        WRONG_ORDER_IDS = [
            '5996711215259',  # #1372
            '5997610598555',  # #1373
            '5998099562651',  # #1374
            '6000732700827',  # #1375
            '6001947771035',  # #1376
            '6009339805851',  # #1377
        ]
        reversed_logs = []
        skipped = []
        for oid in WRONG_ORDER_IDS:
            logs = InventoryLog.query.filter_by(order_id=oid).all()
            if not logs:
                skipped.append(oid)
                continue
            for log in logs:
                if log.qty_change < 0:  # only reverse deductions
                    comp = Component.query.get(log.component_id)
                    if comp:
                        comp.qty -= log.qty_change  # subtracting negative = adding back
                        reversal = InventoryLog(
                            component_id=log.component_id,
                            qty_change=-log.qty_change,
                            reason=f"REPAIR: reversed incorrect deduction from order #{oid} (pre-cutoff order)",
                            user_id=current_user.id
                        )
                        db.session.add(reversal)
                        reversed_logs.append({
                            'order_id': oid, 'part': comp.part_number,
                            'added_back': -log.qty_change, 'new_qty': comp.qty
                        })
            # Remove from ShopifyOrder so future syncs won't try to re-process
            wrong_order = ShopifyOrder.query.filter_by(shopify_order_id=oid).first()
            if wrong_order:
                db.session.delete(wrong_order)
        db.session.commit()
        return jsonify({'ok': True, 'reversed': reversed_logs, 'skipped': skipped})

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

        # Auto-ship: buy label, email Josh, fulfill Shopify
        if result.get('status') not in ('already_processed',) and result.get('deductions'):
            try:
                from shipstation import auto_ship_from_order_data
                auto_ship_from_order_data(order_data)
            except Exception as e:
                current_app.logger.error(f"Auto-ship error: {e}")

        return jsonify(result), 200

    def _auto_ship_from_order(order_data, process_result):
        """Extract ship_to and kit info from order data, buy all labels, then fulfill Shopify once."""
        from shipstation import buy_label_and_notify_josh, fulfill_shopify_order, mark_shipped_v1
        from models import Kit
        order_number = str(order_data.get('order_number', ''))
        shopify_order_id = str(order_data.get('id', ''))
        sa = order_data.get('shipping_address') or order_data.get('billing_address') or {}
        ship_to = {
            'name': sa.get('name', ''),
            'address_line1': sa.get('address1', ''),
            'address_line2': sa.get('address2', '') or '',
            'city_locality': sa.get('city', ''),
            'state_province': sa.get('province_code', sa.get('province', '')),
            'postal_code': sa.get('zip', ''),
            'country_code': sa.get('country_code', 'US'),
            'phone': sa.get('phone', '') or '4805550000',
        }
        order_total = float(order_data.get('total_price', 0) or 0)
        all_line_items = order_data.get('line_items', [])

        # Phase 1: buy a label per kit, collect tracking numbers
        label_results = []
        for item in all_line_items:
            product_id = str(item.get('product_id', ''))
            qty = item.get('quantity', 1)
            variant_title = (item.get('variant_title') or '').lower()
            kits = Kit.query.filter_by(shopify_id=product_id).all()
            if not kits:
                continue
            matched_kit = kits[0]
            if len(kits) > 1:
                for k in kits:
                    if k.shopify_variant and k.shopify_variant.lower() in variant_title:
                        matched_kit = k
                        break
            r = buy_label_and_notify_josh(order_number, matched_kit.name, qty, ship_to,
                                          order_total=order_total, line_items=all_line_items)
            label_results.append(r)
            if r.get('error'):
                current_app.logger.error(f"Label error for {matched_kit.name} on #{order_number}: {r['error']}")

        # Phase 2: fulfill Shopify once with all tracking numbers
        trackings = [r['tracking_number'] for r in label_results if r.get('tracking_number')]
        carrier = next((r.get('carrier', '') for r in label_results if r.get('carrier')), '')
        if trackings:
            try:
                fulfill_shopify_order(shopify_order_id, trackings, carrier)
            except Exception as e:
                current_app.logger.error(f"Shopify fulfillment error for #{order_number}: {e}")
            try:
                mark_shipped_v1(order_number, ', '.join(trackings), carrier)
            except Exception as e:
                current_app.logger.error(f"ShipStation markasshipped error for #{order_number}: {e}")

    @app.route('/api/ship/<shopify_order_id>', methods=['POST'])
    @login_required
    def manual_ship(shopify_order_id):
        """Manually trigger label purchase for a Shopify order."""
        from shipstation import buy_label_and_notify_josh, fulfill_shopify_order, mark_shipped_v1
        from models import Kit
        import requests as req
        token = app.config.get('SHOPIFY_TOKEN', '')
        store = app.config.get('SHOPIFY_STORE', 'edf236-3.myshopify.com')
        r = req.get(
            f"https://{store}/admin/api/2024-01/orders/{shopify_order_id}.json",
            headers={"X-Shopify-Access-Token": token},
            timeout=15
        )
        if not r.ok:
            return jsonify({'error': f'Shopify order fetch failed: {r.status_code}'}), 400
        order_data = r.json().get('order', {})
        sa = order_data.get('shipping_address') or order_data.get('billing_address') or {}
        ship_to = {
            'name': sa.get('name', ''),
            'address_line1': sa.get('address1', ''),
            'address_line2': sa.get('address2', '') or '',
            'city_locality': sa.get('city', ''),
            'state_province': sa.get('province_code', sa.get('province', '')),
            'postal_code': sa.get('zip', ''),
            'country_code': sa.get('country_code', 'US'),
            'phone': sa.get('phone', '') or '4805550000',
        }
        order_number = str(order_data.get('order_number', ''))
        order_total = float(order_data.get('total_price', 0) or 0)
        all_line_items = order_data.get('line_items', [])

        # Phase 1: buy a label per kit
        label_results = []
        for item in all_line_items:
            product_id = str(item.get('product_id', ''))
            qty = item.get('quantity', 1)
            variant_title = (item.get('variant_title') or '').lower()
            kits = Kit.query.filter_by(shopify_id=product_id).all()
            if not kits:
                label_results.append({'item': item.get('title'), 'status': 'no_kit_match'})
                continue
            matched_kit = kits[0]
            if len(kits) > 1:
                for k in kits:
                    if k.shopify_variant and k.shopify_variant.lower() in variant_title:
                        matched_kit = k
                        break
            ship_result = buy_label_and_notify_josh(order_number, matched_kit.name, qty, ship_to,
                                                    order_total=order_total, line_items=all_line_items)
            label_results.append(ship_result)

        # Phase 2: fulfill Shopify once with all tracking numbers
        trackings = [r['tracking_number'] for r in label_results if r.get('tracking_number')]
        carrier = next((r.get('carrier', '') for r in label_results if r.get('carrier')), '')
        fulfill_resp = None
        if trackings:
            try:
                fulfill_resp = fulfill_shopify_order(shopify_order_id, trackings, carrier)
            except Exception as e:
                fulfill_resp = {'error': str(e)}
            try:
                mark_shipped_v1(order_number, ', '.join(trackings), carrier)
            except Exception as e:
                current_app.logger.error(f"ShipStation markasshipped error for #{order_number}: {e}")

        return jsonify({'results': label_results, 'shopify_fulfillment': fulfill_resp})

    # ── Void ShipStation Label ──────────────────────────────────

    @app.route('/api/label/<label_id>/void', methods=['POST'])
    @login_required
    def void_label_route(label_id):
        """Void a purchased ShipStation label and reclaim the credit."""
        from shipstation import void_label
        try:
            result = void_label(label_id)
            return jsonify({'ok': True, 'result': result})
        except Exception as e:
            return jsonify({'error': str(e)}), 400


    @app.route('/api/admin/reset-password', methods=['POST'])
    @login_required
    def admin_reset_password():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        data = request.get_json()
        email = data.get('email', '').lower().strip()
        new_pw = data.get('new_password', '')
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404
        user.set_password(new_pw)
        user.must_change_password = True
        db.session.commit()
        return jsonify({'ok': True, 'email': email})

    @app.route('/api/shipstation/balance', methods=['GET'])
    @login_required
    def shipstation_balance():
        """Return current ShipStation account balance."""
        from shipstation import get_balance
        bal = get_balance()
        return jsonify(bal or {'error': 'Could not fetch balance'})

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

    # ── Turn14 Routes ────────────────────────────────────────────

    @app.route('/api/turn14/inventory')
    @login_required
    def turn14_inventory():
        """Return current Turn14 inventory and pricing for lowering kit parts."""
        try:
            from turn14_sync import sync_lowering_kit_inventory
            results = sync_lowering_kit_inventory()
            return jsonify({'ok': True, 'data': results})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    @app.route('/api/turn14/quote', methods=['POST'])
    @login_required
    def turn14_quote():
        """Get shipping quote for lowering kit to a customer address."""
        from turn14_sync import get_client, LOWERING_KIT_ITEMS
        data = request.get_json()
        ship_to = data.get('ship_to', {})
        item_keys = data.get('items', list(LOWERING_KIT_ITEMS.keys()))
        items = [{'item_id': LOWERING_KIT_ITEMS[k]['id'], 'qty': 1} for k in item_keys if k in LOWERING_KIT_ITEMS]
        try:
            client = get_client()
            quotes = client.get_shipping_quote(items, ship_to)
            return jsonify({'ok': True, 'quotes': quotes})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    @app.route('/api/turn14/order', methods=['POST'])
    @login_required
    def turn14_place_order():
        """Place a Turn14 dropship order and log it for access-expiry tracking."""
        from turn14_sync import get_client
        data = request.get_json()
        po_number = data.get('po_number', '').strip()
        quote_id = data.get('quote_id')
        shipping_ids = data.get('shipping_ids', [])
        environment = data.get('environment', 'production')
        if not po_number or not quote_id or not shipping_ids:
            return jsonify({'error': 'po_number, quote_id, and shipping_ids required'}), 400
        try:
            client = get_client()
            result = client.place_order(po_number, int(quote_id), [int(s) for s in shipping_ids], environment)
            order_id = str(result.get('data', {}).get('id', ''))
            log = Turn14OrderLog(po_number=po_number, t14_order_id=order_id, environment=environment)
            db.session.add(log)
            db.session.commit()
            return jsonify({'ok': True, 'order': result})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ── Dealer Portal ────────────────────────────────────────────

    DEALER_KITS = {
        'fusion_intake': {'name': 'Fusion Sport 2.7L Intake Pipes', 'retail': 385.0},
        'fusion_charge': {'name': 'Fusion Sport 2.7L Charge Pipes', 'retail': 600.0},
    }
    PC_OPTIONS = {
        'raw':           ('Raw Aluminum', 0),
        'crinkle_black': ('Crinkle Black', 100),
        'gloss_black':   ('Gloss Black', 100),
        'satin_black':   ('Satin Black', 100),
        'red':           ('Red', 150),
        'blue':          ('Blue', 150),
        'white':         ('White', 150),
    }
    SHIPPING_MARKUP = 15.0
    DEALER_EMAIL = 'troy@cd3performance.com'
    ADMIN_EMAIL = 'info@ecopowerparts.com'

    def _get_dealer_inv_row(dealer_id, kit_slug):
        """Return single canonical inventory row, deduplicating if needed."""
        rows = DealerInventory.query.filter_by(
            dealer_id=dealer_id, kit_slug=kit_slug
        ).order_by(DealerInventory.id.desc()).all()
        if not rows:
            row = DealerInventory(dealer_id=dealer_id, kit_slug=kit_slug,
                                  kit_name=DEALER_KITS[kit_slug]['name'], sets_on_hand=0)
            db.session.add(row)
            db.session.flush()
            return row
        # Delete older duplicates, keep highest id
        for old in rows[1:]:
            db.session.delete(old)
        return rows[0]

    def _get_or_create_dealer_inventory(dealer_id):
        rows = {}
        for slug in DEALER_KITS:
            rows[slug] = _get_dealer_inv_row(dealer_id, slug)
        db.session.commit()
        return rows

    def _paypal_invoice(order_id, ship_name, invoice_items):
        """
        Create and send a PayPal invoice. invoice_items = [{name, amount}].
        Returns payer_url or None on failure.
        """
        import requests as _req, base64 as _b64
        client_id = current_app.config.get('PAYPAL_CLIENT_ID', '')
        client_secret = current_app.config.get('PAYPAL_CLIENT_SECRET', '')
        if not client_id or not client_secret:
            return None
        base = 'https://api-m.paypal.com'
        # Get token
        creds = _b64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
        tr = _req.post(f'{base}/v1/oauth2/token',
                       headers={'Authorization': f'Basic {creds}', 'Accept': 'application/json'},
                       data='grant_type=client_credentials', timeout=15)
        tr.raise_for_status()
        token = tr.json()['access_token']
        hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                'Prefer': 'return=representation'}
        total = round(sum(i['amount'] for i in invoice_items), 2)
        payload = {
            'detail': {
                'invoice_number': f'CD3-{order_id}',
                'currency_code': 'USD',
                'payment_term': {'term_type': 'DUE_ON_RECEIPT'},
                'note': 'Thank you! Please pay upon receipt.',
            },
            'invoicer': {'name': {'business_name': 'EcoPowerParts'},
                         'email_address': ADMIN_EMAIL},
            'primary_recipients': [{'billing_info': {
                'name': {'given_name': 'Troy', 'surname': 'Walls'},
                'email_address': DEALER_EMAIL,
            }}],
            'items': [
                {'name': i['name'], 'quantity': '1', 'unit_amount': {
                    'currency_code': 'USD', 'value': f"{i['amount']:.2f}"}}
                for i in invoice_items
            ],
            'amount': {'breakdown': {'item_total': {
                'currency_code': 'USD', 'value': f'{total:.2f}'
            }}},
        }
        cr = _req.post(f'{base}/v2/invoicing/invoices', json=payload, headers=hdrs, timeout=20)
        cr.raise_for_status()
        inv_data = cr.json()
        inv_id = inv_data.get('id') or (inv_data.get('href', '').rsplit('/', 1)[-1])
        if not inv_id:
            return None
        # Send the invoice (emails Troy + makes it payable)
        _req.post(f'{base}/v2/invoicing/invoices/{inv_id}/send',
                  json={'send_to_recipient': True}, headers=hdrs, timeout=20)
        # Payer link
        for link in inv_data.get('links', []):
            if link.get('rel') == 'payer-view':
                return link.get('href')
        return f'https://www.paypal.com/invoice/p/#{inv_id}'

    @app.route('/dealer')
    @login_required
    def dealer_portal():
        if current_user.role not in ('dealer', 'admin'):
            flash('Access denied', 'error')
            return redirect(url_for('dashboard'))
        if current_user.role == 'admin':
            dealers = User.query.filter_by(role='dealer').all()
            all_orders = DealerOrder.query.order_by(DealerOrder.created_at.desc()).limit(100).all()
            all_inv = DealerInventory.query.order_by(DealerInventory.id.desc()).all()
            # Dedup for display — only show the row with highest id per (dealer_id, kit_slug)
            seen = set()
            unique_inv = []
            for row in all_inv:
                key = (row.dealer_id, row.kit_slug)
                if key not in seen:
                    seen.add(key)
                    unique_inv.append(row)
            return render_template('dealer.html',
                                   is_admin=True, dealers=dealers,
                                   all_orders=all_orders, all_inv=unique_inv,
                                   dealer_kits=DEALER_KITS, pc_options=PC_OPTIONS)
        # Dealer view
        inv = _get_or_create_dealer_inventory(current_user.id)
        orders = DealerOrder.query.filter_by(dealer_id=current_user.id)\
                                  .order_by(DealerOrder.created_at.desc()).limit(50).all()
        # Outstanding balance = sum of total_owed on shipped (unpaid) orders
        balance_owed = sum(
            (o.total_owed or 0) for o in orders
            if o.order_type == 'dropship' and o.status in ('shipped', 'pending')
               and o.total_owed and o.total_owed > 0
        )
        return render_template('dealer.html',
                               is_admin=False, inv=inv,
                               orders=orders, dealer_kits=DEALER_KITS, pc_options=PC_OPTIONS,
                               balance_owed=balance_owed)

    @app.route('/dealer/view_as/<int:dealer_id>')
    @login_required
    def dealer_view_as(dealer_id):
        """Admin-only: render the dealer portal exactly as that dealer sees it."""
        if current_user.role != 'admin':
            flash('Access denied', 'error')
            return redirect(url_for('dashboard'))
        dealer = User.query.get_or_404(dealer_id)
        inv = _get_or_create_dealer_inventory(dealer_id)
        orders = DealerOrder.query.filter_by(dealer_id=dealer_id)\
                                  .order_by(DealerOrder.created_at.desc()).limit(50).all()
        balance_owed = sum(
            (o.total_owed or 0) for o in orders
            if o.order_type == 'dropship' and o.status in ('shipped', 'pending')
               and o.total_owed and o.total_owed > 0
        )
        return render_template('dealer.html',
                               is_admin=False, inv=inv,
                               orders=orders, dealer_kits=DEALER_KITS, pc_options=PC_OPTIONS,
                               balance_owed=balance_owed,
                               viewing_as=dealer)

    @app.route('/api/dealer/dropship', methods=['POST'])
    @login_required
    def dealer_dropship():
        if current_user.role not in ('dealer', 'admin'):
            return jsonify({'error': 'Access denied'}), 403
        import json as _json
        from shipstation import create_label, email_label_to_josh
        data = request.get_json()
        dealer_id = current_user.id if current_user.role == 'dealer' else int(data.get('dealer_id', current_user.id))

        items = data.get('items', [])
        if not items:
            return jsonify({'error': 'No items selected'}), 400

        ship = data.get('ship_to', {})
        if not ship.get('name') or not ship.get('address1') or not ship.get('city') or not ship.get('zip'):
            return jsonify({'error': 'Ship-to address required'}), 400

        # Validate + enrich items
        pc_total = 0
        enriched = []
        for item in items:
            slug = item.get('kit_slug')
            pc_key = item.get('powder_coat', 'raw')
            if slug not in DEALER_KITS:
                return jsonify({'error': f'Unknown kit: {slug}'}), 400
            if pc_key not in PC_OPTIONS:
                return jsonify({'error': f'Unknown powder coat: {pc_key}'}), 400
            pc_label, pc_cost = PC_OPTIONS[pc_key]
            pc_total += pc_cost
            enriched.append({'kit_slug': slug, 'kit_name': DEALER_KITS[slug]['name'],
                              'powder_coat': pc_key, 'pc_label': pc_label, 'pc_cost': pc_cost})

        # Check inventory
        inv = _get_or_create_dealer_inventory(dealer_id)
        for item in enriched:
            if inv[item['kit_slug']].sets_on_hand < 1:
                return jsonify({'error': f'No {DEALER_KITS[item["kit_slug"]]["name"]} sets on hand'}), 400

        # ShipStation ship_to format
        ss_ship_to = {
            'name': ship.get('name', ''),
            'address_line1': ship.get('address1', ''),
            'city_locality': ship.get('city', ''),
            'state_province': ship.get('state', ''),
            'postal_code': ship.get('zip', ''),
            'country_code': 'US',
            'phone': ship.get('phone', '') or '4805550000',
        }

        # Buy a label per item
        tracking_numbers = []
        label_errors = []
        label_urls = []
        shipping_cost_total = 0.0
        for item in enriched:
            try:
                label = create_label(
                    f"CD3-{data.get('po_ref','0')}", item['kit_name'], 1,
                    ss_ship_to, order_total=0
                )
                tracking = label.get('tracking_number', '')
                tracking_numbers.append(tracking)
                # Extract cost from label response
                cost_obj = label.get('shipment_cost')
                if isinstance(cost_obj, dict):
                    cost = float(cost_obj.get('amount', 0))
                elif cost_obj is not None:
                    cost = float(cost_obj)
                else:
                    cost = 0.0
                shipping_cost_total += cost
                # Store label URL for later PDF attachment
                dl = label.get('label_download', {})
                label_urls.append(dl.get('pdf') or dl.get('href') or '')
                # Email Josh with label attached
                try:
                    email_label_to_josh(
                        f"CD3-{data.get('po_ref','0')}", item['kit_name'],
                        ship.get('name', ''), label, ship_to=ss_ship_to
                    )
                except Exception as e:
                    current_app.logger.error(f'CD3 Josh email error: {e}')
            except Exception as e:
                current_app.logger.error(f'CD3 label error for {item["kit_name"]}: {e}')
                label_errors.append(str(e))

        tracking_str = ', '.join(t for t in tracking_numbers if t)
        # Charge = PC + (actual shipping + $15 markup), no mention of $15 to dealer
        total_charged = round(pc_total + shipping_cost_total + SHIPPING_MARKUP, 2)

        # Save order
        order = DealerOrder(
            dealer_id=dealer_id,
            order_type='dropship',
            status='shipped' if tracking_str else 'pending',
            po_ref=data.get('po_ref', ''),
            ship_to_name=ship.get('name', ''),
            ship_to_address1=ship.get('address1', ''),
            ship_to_city=ship.get('city', ''),
            ship_to_state=ship.get('state', ''),
            ship_to_zip=ship.get('zip', ''),
            ship_to_email=ship.get('email', ''),
            ship_to_phone=ship.get('phone', ''),
            items_json=_json.dumps(enriched),
            pc_cost=pc_total,
            shipping_cost=shipping_cost_total,
            shipping_markup=SHIPPING_MARKUP,
            total_owed=total_charged,
            tracking_number=tracking_str,
            label_url=', '.join(u for u in label_urls if u),
            notes=data.get('notes', ''),
        )
        if tracking_str:
            order.shipped_at = datetime.now(timezone.utc)
        db.session.add(order)

        # Decrement inventory
        for item in enriched:
            inv[item['kit_slug']].sets_on_hand = max(0, inv[item['kit_slug']].sets_on_hand - 1)

        db.session.commit()

        # Build PayPal invoice items (no mention of $15 markup — bundled into shipping)
        paypal_items = []
        if pc_total > 0:
            for item in enriched:
                if item['pc_cost'] > 0:
                    paypal_items.append({'name': f"{item['kit_name']} — {item['pc_label']} Powder Coat",
                                         'amount': float(item['pc_cost'])})
        shipping_charged = round(shipping_cost_total + SHIPPING_MARKUP, 2)
        paypal_items.append({'name': 'Shipping', 'amount': shipping_charged})

        paypal_url = None
        try:
            paypal_url = _paypal_invoice(order.id, ship.get('name', ''), paypal_items)
        except Exception as e:
            current_app.logger.error(f'PayPal invoice error: {e}')

        # Always email Troy: order confirmation + tracking + cost + PayPal link
        try:
            items_desc = ', '.join(i['kit_name'] for i in enriched)
            email_lines = [
                f"Drop-ship order submitted successfully.",
                f"",
                f"Customer: {ship.get('name','')}",
                f"Address: {ship.get('address1','')}, {ship.get('city','')}, {ship.get('state','')} {ship.get('zip','')}",
                f"Items: {items_desc}",
                f"",
            ]
            if tracking_str:
                email_lines += [f"Tracking: {tracking_str}", f""]
            else:
                email_lines += [f"Label is being prepared — tracking will be sent separately.", f""]
            email_lines += [
                f"Amount due: ${total_charged:.2f}",
                f"  {'Powder coat: $' + str(pc_total) + chr(10) + '  ' if pc_total else ''}Shipping: ${shipping_charged:.2f}",
            ]
            if paypal_url:
                email_lines += [f"", f"Pay here: {paypal_url}"]
            else:
                email_lines += [f"", f"A PayPal invoice will be sent to your email shortly."]
            mail.send_message(
                subject=f"Drop-Ship Confirmed — {ship.get('name','')} ({items_desc})",
                recipients=[DEALER_EMAIL],
                body='\n'.join(email_lines)
            )
        except Exception as e:
            current_app.logger.error(f'Dealer order confirmation email error: {e}')

        resp = {
            'ok': True,
            'order_id': order.id,
            'tracking': tracking_str or None,
            'label_errors': label_errors,
            'breakdown': {
                'pc': pc_total,
                'shipping': shipping_charged,
                'total': total_charged,
            },
            'paypal_url': paypal_url,
        }
        return jsonify(resp)

    @app.route('/api/dealer/restock', methods=['POST'])
    @login_required
    def dealer_restock():
        if current_user.role not in ('dealer', 'admin'):
            return jsonify({'error': 'Access denied'}), 403
        data = request.get_json()
        dealer_id = current_user.id if current_user.role == 'dealer' else int(data.get('dealer_id', current_user.id))

        intake_qty = int(data.get('intake_qty', 0))
        charge_qty = int(data.get('charge_qty', 0))
        if intake_qty < 5 or charge_qty < 5:
            return jsonify({'error': 'Minimum 5 sets each of intake and charge pipes required for bulk discount'}), 400

        intake_kit = Kit.query.filter_by(slug='fusion_intake').first()
        charge_kit = Kit.query.filter_by(slug='fusion_charge').first()
        intake_retail = (intake_kit.retail_price if intake_kit and intake_kit.retail_price else DEALER_KITS['fusion_intake']['retail'])
        charge_retail = (charge_kit.retail_price if charge_kit and charge_kit.retail_price else DEALER_KITS['fusion_charge']['retail'])
        discount = 0.20
        total = round((intake_qty * intake_retail + charge_qty * charge_retail) * (1 - discount), 2)

        order = DealerOrder(
            dealer_id=dealer_id, order_type='restock', status='pending',
            intake_qty=intake_qty, charge_qty=charge_qty,
            discount_pct=20, restock_total=total,
            notes=data.get('notes', ''),
        )
        db.session.add(order)
        db.session.commit()

        # PayPal invoice for restock
        paypal_url = None
        try:
            restock_items = [
                {'name': f'Fusion Intake Pipes × {intake_qty} (20% dealer discount)', 'amount': round(intake_qty * intake_retail * 0.8, 2)},
                {'name': f'Fusion Charge Pipes × {charge_qty} (20% dealer discount)', 'amount': round(charge_qty * charge_retail * 0.8, 2)},
            ]
            paypal_url = _paypal_invoice(f'RS-{order.id}', 'Troy Walls', restock_items)
        except Exception as e:
            current_app.logger.error(f'Restock PayPal error: {e}')

        # Email admin
        body = (
            f"Dealer restock request #{order.id}\n"
            f"Dealer: CD3 Performance (troy@cd3performance.com)\n\n"
            f"  Fusion Intake × {intake_qty}: ${intake_qty * intake_retail * 0.8:.2f}\n"
            f"  Fusion Charge × {charge_qty}: ${charge_qty * charge_retail * 0.8:.2f}\n"
            f"  TOTAL DUE: ${total:.2f}\n\n"
            + (f"PayPal invoice: {paypal_url}\n\n" if paypal_url else "")
            + f"Confirm receipt at: https://epp-inventory.onrender.com/dealer"
        )
        try:
            _smtp_send(ADMIN_EMAIL, f'[CD3 Restock] Request #{order.id} — ${total:.2f}', body)
        except Exception as e:
            current_app.logger.error(f'Restock email failed: {e}')

        return jsonify({'ok': True, 'order_id': order.id, 'total': total, 'paypal_url': paypal_url})

    @app.route('/api/dealer/order/<int:order_id>/confirm-restock', methods=['POST'])
    @login_required
    def dealer_confirm_restock(order_id):
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        order = DealerOrder.query.get_or_404(order_id)
        if order.order_type != 'restock':
            return jsonify({'error': 'Not a restock order'}), 400
        if order.status == 'complete':
            return jsonify({'error': 'Already confirmed'}), 400
        inv = _get_or_create_dealer_inventory(order.dealer_id)
        inv['fusion_intake'].sets_on_hand += order.intake_qty
        inv['fusion_charge'].sets_on_hand += order.charge_qty
        order.status = 'complete'
        order.shipped_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({'ok': True,
                        'intake_on_hand': inv['fusion_intake'].sets_on_hand,
                        'charge_on_hand': inv['fusion_charge'].sets_on_hand})

    @app.route('/api/dealer/inventory/set', methods=['POST'])
    @login_required
    def dealer_inventory_set():
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        data = request.get_json()
        dealer_id = int(data.get('dealer_id'))
        kit_slug = data.get('kit_slug')
        qty = int(data.get('qty', 0))
        if kit_slug not in DEALER_KITS:
            return jsonify({'error': 'Invalid kit_slug'}), 400
        row = _get_dealer_inv_row(dealer_id, kit_slug)
        row.sets_on_hand = max(0, qty)
        db.session.commit()
        return jsonify({'ok': True, 'sets_on_hand': row.sets_on_hand})

    @app.route('/api/dealer/order/<int:order_id>/ship', methods=['POST'])
    @login_required
    def dealer_order_ship(order_id):
        """Admin: mark a dealer drop-ship as shipped, calculate total, send PayPal invoice + email to dealer."""
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        import json as _json
        data = request.get_json()
        order = DealerOrder.query.get_or_404(order_id)
        if order.order_type != 'dropship':
            return jsonify({'error': 'Only drop-ship orders can be marked shipped this way'}), 400

        tracking = data.get('tracking', '').strip()
        if not tracking:
            return jsonify({'error': 'Tracking number required'}), 400
        shipping_cost = float(data.get('shipping_cost', 0) or 0)

        order.tracking_number = tracking
        order.shipping_cost = shipping_cost
        order.status = 'shipped'
        order.shipped_at = datetime.now(timezone.utc)

        # Total = powder coat + shipping + $15 markup (markup not disclosed to dealer)
        pc_cost = order.pc_cost or 0
        shipping_charged = round(shipping_cost + SHIPPING_MARKUP, 2)
        total_owed = round(pc_cost + shipping_charged, 2)
        order.total_owed = total_owed
        db.session.commit()

        # Build PayPal invoice
        paypal_items = []
        if pc_cost > 0:
            items = _json.loads(order.items_json or '[]')
            for item in items:
                if item.get('pc_cost', 0) > 0:
                    paypal_items.append({'name': f"{item['kit_name']} — {item.get('pc_label','Powder Coat')}",
                                         'amount': float(item['pc_cost'])})
        paypal_items.append({'name': 'Shipping', 'amount': shipping_charged})

        paypal_url = None
        try:
            paypal_url = _paypal_invoice(order.id, order.ship_to_name or 'Customer', paypal_items)
        except Exception as e:
            current_app.logger.error(f'PayPal invoice error for order {order_id}: {e}')

        items_list = _json.loads(order.items_json or '[]')
        items_desc = ', '.join(i['kit_name'] for i in items_list)

        # Email Josh with label PDF attached
        try:
            from shipstation import email_label_to_josh as _etj, _fetch_label_pdf
            # Construct minimal label_data so email_label_to_josh can attach PDF
            label_data = {
                'tracking_number': tracking,
                'carrier_code': 'ups',
                'service_code': 'ups_ground',
                'label_download': {'pdf': order.label_url or '', 'href': order.label_url or ''},
            }
            ss_ship_to = {
                'name': order.ship_to_name,
                'address_line1': order.ship_to_address1,
                'city_locality': order.ship_to_city,
                'state_province': order.ship_to_state,
                'postal_code': order.ship_to_zip,
                'country_code': 'US',
                'phone': order.ship_to_phone or '',
            }
            _etj(f"CD3-{order_id}", items_desc, order.ship_to_name, label_data, ship_to=ss_ship_to)
        except Exception as e:
            current_app.logger.error(f'Josh ship email error for order {order_id}: {e}')

        # Email dealer: shipped notice + PayPal
        try:
            body_lines = [
                f"Your drop-ship order has been fulfilled and is on its way!",
                f"",
                f"Customer: {order.ship_to_name}",
                f"Ship-to: {order.ship_to_address1}, {order.ship_to_city}, {order.ship_to_state} {order.ship_to_zip}",
                f"Items: {items_desc}",
                f"Tracking: {tracking}",
                f"",
                f"Amount owed: ${total_owed:.2f}",
            ]
            if paypal_url:
                body_lines += [f"", f"Pay here: {paypal_url}"]
            mail.send_message(
                subject=f"Order Shipped — {order.ship_to_name} ({items_desc})",
                recipients=[DEALER_EMAIL],
                body='\n'.join(body_lines)
            )
        except Exception as e:
            current_app.logger.error(f'Dealer ship email error for order {order_id}: {e}')

        return jsonify({'ok': True, 'total_owed': total_owed, 'paypal_url': paypal_url})

    @app.route('/api/dealer/order/<int:order_id>/patch', methods=['POST'])
    @login_required
    def dealer_order_patch(order_id):
        """Admin: manually correct a dealer order's tracking, cost, and status."""
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        data = request.get_json()
        order = DealerOrder.query.get_or_404(order_id)
        if 'tracking' in data:
            order.tracking_number = data['tracking']
        if 'shipping_cost' in data:
            order.shipping_cost = float(data['shipping_cost'])
        if 'pc_cost' in data:
            order.pc_cost = float(data['pc_cost'])
        if 'total_owed' in data:
            order.total_owed = float(data['total_owed'])
        if 'status' in data:
            order.status = data['status']
        if 'po_ref' in data:
            order.po_ref = data['po_ref']
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/dealer/log-historical', methods=['POST'])
    @login_required
    def dealer_log_historical():
        """Admin: log a historical dealer order without buying a label."""
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        import json as _json
        data = request.get_json()
        dealer_id = int(data.get('dealer_id', 3))
        enriched = data.get('items', [])
        ship = data.get('ship_to', {})
        order = DealerOrder(
            dealer_id=dealer_id,
            order_type='dropship',
            status=data.get('status', 'shipped'),
            po_ref=data.get('po_ref', ''),
            ship_to_name=ship.get('name', ''),
            ship_to_address1=ship.get('address1', ''),
            ship_to_city=ship.get('city', ''),
            ship_to_state=ship.get('state', ''),
            ship_to_zip=ship.get('zip', ''),
            ship_to_email=ship.get('email', ''),
            ship_to_phone=ship.get('phone', ''),
            items_json=_json.dumps(enriched),
            pc_cost=float(data.get('pc_cost', 0)),
            shipping_cost=float(data.get('shipping_cost', 0)),
            shipping_markup=SHIPPING_MARKUP,
            total_owed=float(data.get('total_owed', 0)),
            tracking_number=data.get('tracking', ''),
            notes=data.get('notes', ''),
        )
        if order.status == 'shipped':
            order.shipped_at = datetime.now(timezone.utc)
        db.session.add(order)
        db.session.commit()
        return jsonify({'ok': True, 'order_id': order.id})

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
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
