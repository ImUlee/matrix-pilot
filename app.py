from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from datetime import datetime, timedelta
import requests
import threading
import time

app = Flask(__name__)

# =========================================
# 一、 数据库配置与自动修复
# =========================================
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///matrix_pilot.db'
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
        
        # 自动检查并修复缺失字段 (针对 OperationalError)
        with db.engine.connect() as conn:
            columns = [row[1] for row in conn.execute(text("PRAGMA table_info(settings)")).fetchall()]
            if 'bark_title' not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN bark_title VARCHAR(100) DEFAULT 'MatrixPilot 提醒'"))
            if 'bark_body' not in columns:
                conn.execute(text("ALTER TABLE settings ADD COLUMN bark_body VARCHAR(255) DEFAULT '分组【{group}】预计下轮时间已到！'"))
            conn.commit()

        # 初始化默认数据
        if not Settings.query.first():
            db.session.add(Settings())
            db.session.commit()

init_db()

# =========================================
# 二、 Bark 推送逻辑
# =========================================
def send_bark_notification(group_name, target_time_str):
    with app.app_context():
        settings = Settings.query.first()
        if not settings or not settings.bark_url: return
        try:
            target_time = datetime.strptime(target_time_str, '%Y-%m-%d %H:%M')
            while True:
                if datetime.now() >= target_time:
                    title = settings.bark_title.replace("{group}", group_name).replace("{time}", target_time_str)
                    body = settings.bark_body.replace("{group}", group_name).replace("{time}", target_time_str)
                    api_url = f"{settings.bark_url.rstrip('/')}/{title}/{body}?group=MatrixPilot&isArchive=1"
                    requests.get(api_url, timeout=10)
                    break
                time.sleep(30)
        except Exception as e:
            print(f"推送异常: {e}")

# =========================================
# 三、 路由逻辑
# =========================================
@app.route('/', methods=['GET', 'POST'])
def index():
    settings = Settings.query.first()
    if request.method == 'POST':
        date_str = request.form.get('date').replace('T', ' ')
        group_name = request.form.get('group')
        quantity = request.form.get('quantity')
        dt_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
        next_time_str = (dt_obj + timedelta(hours=settings.interval_hours)).strftime('%Y-%m-%d %H:%M')
        new_record = Record(date=date_str, next_time=next_time_str, data={group_name: quantity})
        db.session.add(new_record)
        db.session.commit()
        if settings.bark_url:
            threading.Thread(target=send_bark_notification, args=(group_name, next_time_str), daemon=True).start()
        return redirect(url_for('index'))

    items = Item.query.all()
    records = Record.query.order_by(Record.date.desc()).all()
    return render_template('index.html', items=items, records=records, 
                           now_str=datetime.now().strftime('%Y-%m-%dT%H:%M'),
                           current_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
                           interval_hours=settings.interval_hours,
                           bark_url=settings.bark_url,
                           bark_title=settings.bark_title,
                           bark_body=settings.bark_body)

@app.route('/edit/<int:id>', methods=['POST'])
def edit_record(id):
    record = Record.query.get_or_404(id)
    settings = Settings.query.first()
    new_date = request.form.get('date').replace('T', ' ')
    record.date = new_date
    record.next_time = (datetime.strptime(new_date, '%Y-%m-%d %H:%M') + timedelta(hours=settings.interval_hours)).strftime('%Y-%m-%d %H:%M')
    record.data = {list(record.data.keys())[0]: request.form.get('value')}
    db.session.commit()
    return redirect(url_for('index', tab='log'))

@app.route('/delete_record/<int:id>', methods=['POST'])
def delete_record(id):
    db.session.delete(Record.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('index', tab='log'))

@app.route('/settings', methods=['POST'])
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