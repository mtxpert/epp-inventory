from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(20), default='user')  # admin or user
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Component(db.Model):
    __tablename__ = 'components'
    id = db.Column(db.Integer, primary_key=True)
    part_number = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(30), nullable=False)  # pipes, couplers, clamps, misc
    old_pn = db.Column(db.String(30), default='')
    qty = db.Column(db.Integer, default=0)
    reorder_threshold = db.Column(db.Integer, default=10)
    unit_cost = db.Column(db.Float, default=0)  # latest weighted average cost

    kit_components = db.relationship('KitComponent', back_populates='component')
    logs = db.relationship('InventoryLog', back_populates='component', order_by='InventoryLog.created_at.desc()')


class Kit(db.Model):
    __tablename__ = 'kits'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    shopify_id = db.Column(db.String(30))
    shopify_variant = db.Column(db.String(30))
    retail_price = db.Column(db.Float, default=0)

    components = db.relationship('KitComponent', back_populates='kit', cascade='all, delete-orphan')


class KitComponent(db.Model):
    __tablename__ = 'kit_components'
    id = db.Column(db.Integer, primary_key=True)
    kit_id = db.Column(db.Integer, db.ForeignKey('kits.id'), nullable=False)
    component_id = db.Column(db.Integer, db.ForeignKey('components.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    kit = db.relationship('Kit', back_populates='components')
    component = db.relationship('Component', back_populates='kit_components')


class InventoryLog(db.Model):
    __tablename__ = 'inventory_log'
    id = db.Column(db.Integer, primary_key=True)
    component_id = db.Column(db.Integer, db.ForeignKey('components.id'), nullable=False)
    qty_change = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(200))
    order_id = db.Column(db.String(50))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    component = db.relationship('Component', back_populates='logs')
    user = db.relationship('User')


class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    contact_name = db.Column(db.String(80), default='')
    notes = db.Column(db.Text, default='')

    components = db.relationship('SupplierComponent', back_populates='supplier')
    purchase_orders = db.relationship('PurchaseOrder', back_populates='supplier')


class SupplierComponent(db.Model):
    __tablename__ = 'supplier_components'
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    component_id = db.Column(db.Integer, db.ForeignKey('components.id'), nullable=False)
    unit_cost = db.Column(db.Float, default=0)

    supplier = db.relationship('Supplier', back_populates='components')
    component = db.relationship('Component')


class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(30), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    status = db.Column(db.String(20), default='draft')  # draft, sent, received, cancelled
    notes = db.Column(db.Text, default='')
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    sent_at = db.Column(db.DateTime)
    received_at = db.Column(db.DateTime)

    supplier = db.relationship('Supplier', back_populates='purchase_orders')
    creator = db.relationship('User')
    lines = db.relationship('PurchaseOrderLine', back_populates='purchase_order', cascade='all, delete-orphan')

    @property
    def total(self):
        return sum((l.qty * (l.unit_cost or 0)) for l in self.lines)


class PurchaseOrderLine(db.Model):
    __tablename__ = 'purchase_order_lines'
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'), nullable=False)
    component_id = db.Column(db.Integer, db.ForeignKey('components.id'), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Float, default=0)

    purchase_order = db.relationship('PurchaseOrder', back_populates='lines')
    component = db.relationship('Component')


class InventorySnapshot(db.Model):
    """Year-end inventory valuation snapshot for accounting."""
    __tablename__ = 'inventory_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    snapshot_date = db.Column(db.Date, nullable=False)
    total_retail_value = db.Column(db.Float, default=0)
    total_cost_value = db.Column(db.Float, default=0)
    details_json = db.Column(db.Text)  # JSON: per-component breakdown
    emailed_to = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Invoice(db.Model):
    """Supplier invoice for COGS tracking."""
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    invoice_number = db.Column(db.String(50), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    total_amount = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, default='')
    file_data = db.Column(db.Text)  # base64 encoded PDF/image
    file_name = db.Column(db.String(200))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    supplier = db.relationship('Supplier')
    creator = db.relationship('User')
    lines = db.relationship('InvoiceLine', back_populates='invoice', cascade='all, delete-orphan')


class InvoiceLine(db.Model):
    """Individual line item on a supplier invoice."""
    __tablename__ = 'invoice_lines'
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)
    component_id = db.Column(db.Integer, db.ForeignKey('components.id'), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)

    invoice = db.relationship('Invoice', back_populates='lines')
    component = db.relationship('Component')

    @property
    def line_total(self):
        return self.qty * self.unit_cost


class Turn14OrderLog(db.Model):
    """Tracks every order placed via Turn14 API — used to enforce 60-day access rule."""
    __tablename__ = 'turn14_order_log'
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), nullable=False)
    t14_order_id = db.Column(db.String(50))
    environment = db.Column(db.String(20), default='production')
    placed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ShopifyOrder(db.Model):
    __tablename__ = 'shopify_orders'
    id = db.Column(db.Integer, primary_key=True)
    shopify_order_id = db.Column(db.String(30), unique=True, nullable=False)
    order_number = db.Column(db.String(30))
    total_price = db.Column(db.String(20))
    processed = db.Column(db.Boolean, default=False)
    line_items_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    processed_at = db.Column(db.DateTime)
