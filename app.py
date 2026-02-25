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
app.secret_key = os.environ.get('SECRET_KEY', 'matrix_pilot_super_secret_key')
app.permanent_session_lifetime = timedelta(days=30)
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
    # 新增字段：标记是否已推送过，防止重复推送和重启丢失
    notified = db.Column(db.Boolean, default=False)          

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
                
                # 自动升级 Record 表，增加 notified 字段
                rec_result = conn.execute(text("PRAGMA table_info(record)")).fetchall()
                rec_cols = [row[1] for row in rec_result]
                if 'notified' not in rec_cols:
                    conn.execute(text("ALTER TABLE record ADD COLUMN notified BOOLEAN DEFAULT 0"))
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

@app.route('/sw.js')
def serve_sw():
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')

# =========================================
# 三、 Bark 全局守护线程 (坚如磐石的推送引擎)
# =========================================
def notification_daemon():
    """后台持续轮询，接管所有推送任务，服务器重启也不丢失"""
    time.sleep(5) # 延迟启动等待数据库准备完毕
    while True:
        try:
            with app.app_context():
                settings = Settings.query.first()
                if settings and settings.bark_url:
                    # 使用服务器当前时间与记录做比对
                    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
                    # 查出所有未通知且到达时间的记录
                    pending_records = Record.query.filter(
                        Record.next_time != '--',
                        Record.next_time != None,
                        Record.notified == False
                    ).all()
                    
                    for r in pending_records:
                        if now_str >= r.next_time:
                            # 悲观锁更新，防止多线程重复发
                            updated = Record.query.filter_by(id=r.id, notified=False).update({'notified': True})
                            db.session.commit()
                            if updated:
                                group_name = list(r.data.keys())[0]
                                title = settings.bark_title.replace("{group}", group_name).replace("{time}", r.next_time)
                                body = settings.bark_body.replace("{group}", group_name).replace("{time}", r.next_time)
                                api_url = f"{settings.bark_url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
                                try:
                                    requests.get(api_url, timeout=10)
                                except Exception as e:
                                    print(f"Bark Push Error: {e}")
        except Exception as e:
            print(f"Daemon Loop Error: {e}")
        time.sleep(60) # 每分钟巡检一次

# 启动后台守护引擎
threading.Thread(target=notification_daemon, daemon=True).start()

# =========================================
# 四、 路由逻辑
# =========================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if pin == APP_PIN:
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = "访问密码错误"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

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
        
        new_record = Record(date=date_str, next_time=next_time_str, data={group_name: quantity}, notified=False)
        db.session.add(new_record)
        db.session.commit()
        return redirect(url_for('index'))

    items = Item.query.all()
    records = Record.query.order_by(Record.date.desc()).all()
    return render_template('index.html', 
                           items=items, 
                           records=records, 
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
    # 修改时间后，重置推送状态，以便重新计算
    record.notified = False
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

# --- 新增：Bark 实时测试路由 ---
@app.route('/test_bark', methods=['POST'])
@login_required
def test_bark():
    url = request.form.get('bark_url')
    if not url:
        return "请先填写 Bark URL", 400
    
    title = request.form.get('bark_title', '测试').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    body = request.form.get('bark_body', '成功连通！').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    
    api_url = f"{url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
    try:
        r = requests.get(api_url, timeout=5)
        if r.status_code == 200:
            return "测试推送成功，请查看手机！", 200
        return f"接口拒绝请求 (HTTP {r.status_code})", 400
    except Exception as e:
        return f"无法连接到 Bark: {str(e)}", 400

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)