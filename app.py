import os
import time
import threading
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session, jsonify
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
                cols = [row[1] for row in result]
                if 'bark_title' not in cols:
                    conn.execute(text("ALTER TABLE settings ADD COLUMN bark_title VARCHAR(100) DEFAULT 'MatrixPilot 提醒'"))
                if 'bark_body' not in cols:
                    conn.execute(text("ALTER TABLE settings ADD COLUMN bark_body VARCHAR(255) DEFAULT '分组【{group}】预计下轮时间已到！'"))
                
                rec_result = conn.execute(text("PRAGMA table_info(record)")).fetchall()
                rec_cols = [row[1] for row in rec_result]
                if 'notified' not in rec_cols:
                    conn.execute(text("ALTER TABLE record ADD COLUMN notified BOOLEAN DEFAULT 0"))
                conn.commit()
        except Exception as e:
            pass

        if not Settings.query.first():
            db.session.add(Settings())
            db.session.commit()

init_db()

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
# 二、 Bark 守护线程
# =========================================
def notification_daemon():
    time.sleep(5) 
    while True:
        try:
            with app.app_context():
                settings = Settings.query.first()
                if settings and settings.bark_url:
                    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
                    pending_records = Record.query.filter(
                        Record.next_time != '--',
                        Record.next_time != None,
                        Record.notified == False
                    ).all()
                    
                    for r in pending_records:
                        if now_str >= r.next_time:
                            updated = Record.query.filter_by(id=r.id, notified=False).update({'notified': True})
                            db.session.commit()
                            if updated:
                                group_name = list(r.data.keys())[0]
                                title = settings.bark_title.replace("{group}", group_name).replace("{time}", r.next_time)
                                body = settings.bark_body.replace("{group}", group_name).replace("{time}", r.next_time)
                                api_url = f"{settings.bark_url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
                                try:
                                    requests.get(api_url, timeout=10)
                                except:
                                    pass
        except:
            pass
        time.sleep(60) 

threading.Thread(target=notification_daemon, daemon=True).start()

# =========================================
# 三、 基础页面路由
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

@app.route('/')
@login_required
def index():
    # 彻底改用 Vue 单页，后端只负责首次下发空壳 HTML
    return render_template('index.html')


# =========================================
# 四、 API 接口 (供 Vue.js + Axios 调用)
# =========================================
@app.route('/api/data', methods=['GET'])
@login_required
def get_data():
    settings = Settings.query.first()
    items = Item.query.all()
    records = Record.query.order_by(Record.date.desc()).all()
    
    return jsonify({
        'settings': {
            'interval_hours': settings.interval_hours,
            'bark_url': settings.bark_url or '',
            'bark_title': settings.bark_title or '',
            'bark_body': settings.bark_body or ''
        },
        'items': [{'id': i.id, 'name': i.name} for i in items],
        'records': [{
            'id': r.id, 
            'date': r.date, 
            'next_time': r.next_time, 
            'group': list(r.data.keys())[0] if r.data else '',
            'quantity': list(r.data.values())[0] if r.data else 0
        } for r in records]
    })

@app.route('/api/record', methods=['POST'])
@login_required
def add_record():
    data = request.json
    settings = Settings.query.first()
    date_str = data.get('date').replace('T', ' ')
    dt_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
    next_dt = dt_obj + timedelta(hours=settings.interval_hours)
    
    new_record = Record(
        date=date_str, 
        next_time=next_dt.strftime('%Y-%m-%d %H:%M'), 
        data={data.get('group'): data.get('quantity')}, 
        notified=False
    )
    db.session.add(new_record)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/record/<int:id>', methods=['PUT'])
@login_required
def edit_record_api(id):
    data = request.json
    record = Record.query.get_or_404(id)
    settings = Settings.query.first()
    new_date = data.get('date').replace('T', ' ')
    
    record.date = new_date
    record.next_time = (datetime.strptime(new_date, '%Y-%m-%d %H:%M') + timedelta(hours=settings.interval_hours)).strftime('%Y-%m-%d %H:%M')
    record.notified = False
    
    old_key = list(record.data.keys())[0]
    record.data = {old_key: data.get('value')}
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/record/<int:id>', methods=['DELETE'])
@login_required
def delete_record_api(id):
    db.session.delete(Record.query.get_or_404(id))
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/settings', methods=['POST'])
@login_required
def save_settings():
    data = request.json
    action = data.get('action')
    settings = Settings.query.first()
    
    if action == 'add_item':
        name = data.get('name')
        if name and not Item.query.filter_by(name=name).first():
            db.session.add(Item(name=name))
    elif action == 'edit_item':
        item = Item.query.get(data.get('id'))
        new_name = data.get('name')
        if item and new_name and item.name != new_name:
            if not Item.query.filter_by(name=new_name).first():
                old_name = item.name
                item.name = new_name
                # 同步迁移历史记录
                for r in Record.query.all():
                    if r.data and old_name in r.data:
                        new_data = dict(r.data)
                        new_data[new_name] = new_data.pop(old_name)
                        r.data = new_data
    elif action == 'delete_item':
        item = Item.query.get(data.get('id'))
        if item: db.session.delete(item)
    elif action == 'update_interval':
        settings.interval_hours = int(data.get('interval_hours'))
    elif action == 'update_bark':
        settings.bark_url = data.get('bark_url')
        settings.bark_title = data.get('bark_title')
        settings.bark_body = data.get('bark_body')
        
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/test_bark', methods=['POST'])
@login_required
def test_bark_api():
    data = request.json
    url = data.get('bark_url')
    if not url: return jsonify({'error': '请先填写 Bark URL'}), 400
    
    title = data.get('bark_title', '测试').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    body = data.get('bark_body', '成功连通！').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    api_url = f"{url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
    try:
        r = requests.get(api_url, timeout=5)
        if r.status_code == 200:
            return jsonify({'msg': '测试推送成功，请查看手机！'})
        return jsonify({'error': f'接口拒绝请求 (HTTP {r.status_code})'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)