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
