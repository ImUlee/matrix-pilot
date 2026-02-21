import os
import time
import threading
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

app = Flask(__name__)

# --- 安全与会话配置 ---
# 必须设置 secret_key 才能使用 session，有效期设置为 30 天
app.secret_key = os.environ.get('SECRET_KEY', 'matrix_pilot_super_secret_key')
app.permanent_session_lifetime = timedelta(days=30)
# 获取环境变量中的访问密码，默认 123456
APP_PIN = os.environ.get('APP_PIN', '123456')

# =========================================
# 一、 数据库配置与持久化路径
# =========================================
INSTANCE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance')
if not os.path.exists(INSTANCE_PATH):
    os.makedirs(INSTANCE_PATH)

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(INSTANCE_PATH, 'matrix_pilot.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(50))      
    next_time = db.Column(db.String(50)) 
    data = db.Column(db.JSON)           

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    interval_hours = db.Column(db.Integer, default=72)
    bark_url = db.Column(db.String(255))
    bark_title = db.Column(db.String(100), default="MatrixPilot 提醒")
    bark_body = db.Column(db.String(255), default="分组【{group}】预计下轮时间已到！")

def init_db():
    with app.app_context():
        db.create_all()
        try:
            with db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA table_info(settings)")).fetchall()
                columns = [row[1] for row in result]
                if 'bark_title' not in columns:
                    conn.execute(text("ALTER TABLE settings ADD COLUMN bark_title VARCHAR(100) DEFAULT 'MatrixPilot 提醒'"))
                if 'bark_body' not in columns:
                    conn.execute(text("ALTER TABLE settings ADD COLUMN bark_body VARCHAR(255) DEFAULT '分组【{group}】预计下轮时间已到！'"))
                conn.commit()
        except Exception as e:
            print(f"数据库补丁执行跳过: {e}")

        if not Settings.query.first():
            db.session.add(Settings())
            db.session.commit()

init_db()

# =========================================
# 二、 权限拦截器 (Login Required)
# =========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# =========================================
# 三、 PWA 与 Bark 异步推送
# =========================================
@app.route('/sw.js')
def serve_sw():
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')

def async_bark_task(group_name, target_time_str):
    with app.app_context():
        settings = Settings.query.first()
        if not settings or not settings.bark_url: return
        try:
            target_time = datetime.strptime(target_time_str, '%Y-%m-%d %H:%M')
            while True:
                if datetime.now() >= target_time:
                    title = settings.bark_title.replace("{group}", group_name).replace("{time}", target_time_str)
                    body = settings.bark_body.replace("{group}", group_name).replace("{time}", target_time_str)
                    api_url = f"{settings.bark_url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
                    requests.get(api_url, timeout=10)
                    break
                time.sleep(30)
        except Exception as e:
            print(f"Bark 推送任务失败: {e}")

# =========================================
# 四、 路由逻辑 (全部加上权限保护)
# =========================================

# --- 登录与登出路由 ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if pin == APP_PIN:
            session.permanent = True  # 开启 30 天免登录
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = "访问密码错误"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# --- 业务路由 ---
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    settings = Settings.query.first()
    if request.method == 'POST':
        date_str = request.form.get('date').replace('T', ' ')
        group_name = request.form.get('group')
        quantity = request.form.get('quantity')
        
        dt_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
        next_dt = dt_obj + timedelta(hours=settings.interval_hours)
        next_time_str = next_dt.strftime('%Y-%m-%d %H:%M')
        
        new_record = Record(date=date_str, next_time=next_time_str, data={group_name: quantity})
        db.session.add(new_record)
        db.session.commit()

        if settings.bark_url:
            thread = threading.Thread(target=async_bark_task, args=(group_name, next_time_str))
            thread.daemon = True
            thread.start()

        return redirect(url_for('index'))

    items = Item.query.all()
    records = Record.query.order_by(Record.date.desc()).all()
    return render_template('index.html', 
                           items=items, 
                           records=records, 
                           now_str=datetime.now().strftime('%Y-%m-%dT%H:%M'),
                           current_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
                           interval_hours=settings.interval_hours,
                           bark_url=settings.bark_url,
                           bark_title=settings.bark_title,
                           bark_body=settings.bark_body)

@app.route('/edit/<int:id>', methods=['POST'])
@login_required
def edit_record(id):
    record = Record.query.get_or_404(id)
    settings = Settings.query.first()
    new_date = request.form.get('date').replace('T', ' ')
    new_val = request.form.get('value')
    record.date = new_date
    record.next_time = (datetime.strptime(new_date, '%Y-%m-%d %H:%M') + timedelta(hours=settings.interval_hours)).strftime('%Y-%m-%d %H:%M')
    old_key = list(record.data.keys())[0]
    record.data = {old_key: new_val}
    db.session.commit()
    return redirect(url_for('index', tab='log'))

@app.route('/delete_record/<int:id>', methods=['POST'])
@login_required
def delete_record(id):
    db.session.delete(Record.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('index', tab='log'))

@app.route('/settings', methods=['POST'])
@login_required
def update_settings():
    action = request.form.get('action')
    settings = Settings.query.first()
    if action == 'add_item':
        name = request.form.get('name')
        if name and not Item.query.filter_by(name=name).first():
            db.session.add(Item(name=name))
    elif action == 'delete_item':
        item = Item.query.get(request.form.get('id'))
        if item: db.session.delete(item)
    elif action == 'update_interval':
        settings.interval_hours = int(request.form.get('interval_hours'))
    elif action == 'update_bark':
        settings.bark_url = request.form.get('bark_url')
        settings.bark_title = request.form.get('bark_title')
        settings.bark_body = request.form.get('bark_body')
    db.session.commit()
    return redirect(url_for('index', tab='settings'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)