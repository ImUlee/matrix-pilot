import os
import sys
import time
import threading
import requests
import sqlite3
import re
import json
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

app = Flask(__name__)

app.secret_key = os.environ.get('SECRET_KEY', 'matrix_pilot_super_secret_key')
app.permanent_session_lifetime = timedelta(days=30)
APP_PIN = os.environ.get('APP_PIN', '123456')

INSTANCE_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance')
if not os.path.exists(INSTANCE_PATH):
    os.makedirs(INSTANCE_PATH)

# =====================================================================
# 模块一：MatrixPilot 数据库与模型
# =====================================================================
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

def init_mp_db():
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
        except Exception:
            pass
        if not Settings.query.first():
            db.session.add(Settings())
            db.session.commit()

init_mp_db()

# =====================================================================
# 模块二：LittlePilot 数据库与解析器引擎
# =====================================================================
LP_DB_PATH = os.path.join(INSTANCE_PATH, 'lottery.db')
ROUND_SETTINGS_FILE = os.path.join(INSTANCE_PATH, 'round_settings.json')
MONTH_MAP = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6, 'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}

# 精简：去除后缀名称
LOG_PARSERS = {
    "default": { "name": "万花筒", "pattern": r"\[(.*?)\]\s+(.*?)_\d+\s+\|.*?[,，]\s*(?:.*?)[,，]\s*(\d+)", "item_type": "钻石", "file_rule": "lot.txt", "folder_rule": "" },
    "qilin": { "name": "麒麟", "pattern": r"\[(.*?)\]\s*恭喜\[(.*?)\].*?中了-(\d+)-", "item_type": "钻石", "file_rule": "*qiling.txt", "folder_rule": "logs" },
    "pixiu": { "name": "貔貅", "pattern": r"^(.*?)----.*?----.*?----(.*?)----(.*)$", "item_type": "动态", "file_rule": "*中奖记录.txt", "folder_rule": "中奖记录" }
}

def load_round_times():
    if os.path.exists(ROUND_SETTINGS_FILE):
        try:
            with open(ROUND_SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: return {}
    return {}

def save_round_times(data):
    try:
        with open(ROUND_SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=4)
    except: pass

round_start_times = load_round_times()

def parse_log_date(date_str):
    try:
        date_str = date_str.strip()
        if '/' in date_str and re.search(r'[a-zA-Z]', date_str):
            parts = date_str.split(); d_parts = parts[0].split('/'); t_parts = parts[1].split(':')
            return datetime(int(d_parts[2]), MONTH_MAP.get(d_parts[1], 0), int(d_parts[0]), int(t_parts[0]), int(t_parts[1]), int(t_parts[2]))
        if '-' in date_str: return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        if '/' in date_str: return datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")
        if '.' in date_str: return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")
        return None
    except: return None

def init_lp_db():
    conn = sqlite3.connect(LP_DB_PATH); conn.row_factory = sqlite3.Row; c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, log_time TEXT, nickname TEXT, item_type TEXT, quantity INTEGER, unique_sign TEXT UNIQUE, device_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, nickname TEXT, last_seen REAL, process_running INTEGER, first_seen REAL, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (date TEXT, device_id TEXT, manual_users INTEGER, manual_sum INTEGER, PRIMARY KEY (date, device_id))''')
    try: c.execute("ALTER TABLE devices ADD COLUMN template_id TEXT DEFAULT 'default'")
    except: pass
    try: c.execute("ALTER TABLE devices ADD COLUMN last_msg TEXT DEFAULT '正常'")
    except: pass
    try: c.execute("ALTER TABLE devices ADD COLUMN detected_template TEXT DEFAULT ''")
    except: pass
    try: c.execute("ALTER TABLE logs ADD COLUMN template_id TEXT DEFAULT 'default'")
    except: pass
    
    c.execute("PRAGMA table_info(daily_overrides)")
    if 'template_id' not in [col['name'] for col in c.fetchall()]:
        c.execute("ALTER TABLE daily_overrides RENAME TO daily_overrides_old")
        c.execute('''CREATE TABLE daily_overrides (date TEXT, device_id TEXT, template_id TEXT DEFAULT 'default', manual_users INTEGER, manual_sum INTEGER, PRIMARY KEY (date, device_id, template_id))''')
        c.execute("INSERT INTO daily_overrides (date, device_id, manual_users, manual_sum) SELECT date, device_id, manual_users, manual_sum FROM daily_overrides_old")
        c.execute("DROP TABLE daily_overrides_old")

    try: c.execute('''DELETE FROM logs WHERE id NOT IN (SELECT MIN(id) FROM logs GROUP BY log_time, nickname, quantity, device_id)''')
    except: pass
    conn.commit(); conn.close()

def get_lp_db_connection():
    conn = sqlite3.connect(LP_DB_PATH); conn.row_factory = sqlite3.Row
    try: c = conn.cursor(); c.execute("SELECT 1 FROM devices LIMIT 1")
    except sqlite3.OperationalError: conn.close(); init_lp_db(); conn = sqlite3.connect(LP_DB_PATH); conn.row_factory = sqlite3.Row
    return conn

init_lp_db()

def update_device_status(device_id, nickname, process_running, password):
    conn = get_lp_db_connection(); c = conn.cursor(); now = time.time()
    c.execute("UPDATE devices SET nickname=?, last_seen=?, process_running=?, password=? WHERE device_id=?", (nickname, now, process_running, password, device_id))
    if c.rowcount == 0: c.execute("INSERT INTO devices (device_id, nickname, last_seen, process_running, first_seen, password, template_id, last_msg, detected_template) VALUES (?, ?, ?, ?, ?, ?, 'default', '正常', '')", (device_id, nickname, now, process_running, now, password))
    conn.commit(); conn.close()

# =====================================================================
# 模块三：权限拦截与通知
# =====================================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'): return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def notification_daemon():
    time.sleep(5) 
    while True:
        try:
            with app.app_context():
                settings = Settings.query.first()
                if settings and settings.bark_url:
                    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
                    pending_records = Record.query.filter(Record.next_time != '--', Record.next_time != None, Record.notified == False).all()
                    for r in pending_records:
                        if now_str >= r.next_time:
                            updated = Record.query.filter_by(id=r.id, notified=False).update({'notified': True})
                            db.session.commit()
                            if updated:
                                group_name = list(r.data.keys())[0]
                                title = settings.bark_title.replace("{group}", group_name).replace("{time}", r.next_time)
                                body = settings.bark_body.replace("{group}", group_name).replace("{time}", r.next_time)
                                api_url = f"{settings.bark_url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
                                try: requests.get(api_url, timeout=10)
                                except: pass
        except: pass
        time.sleep(60) 

threading.Thread(target=notification_daemon, daemon=True).start()

@app.route('/sw.js')
def serve_sw(): return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if pin == APP_PIN:
            session.permanent = True; session['logged_in'] = True
            return redirect(url_for('index'))
        else: error = "访问密码错误"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout(): session.pop('logged_in', None); return redirect(url_for('login'))

@app.route('/')
@login_required
def index(): return render_template('index.html')


# =====================================================================
# API 路由
# =====================================================================
@app.route('/api/data', methods=['GET'])
@login_required
def get_data():
    settings = Settings.query.first(); items = Item.query.all(); records = Record.query.order_by(Record.date.desc()).all()
    return jsonify({
        'settings': {'interval_hours': settings.interval_hours, 'bark_url': settings.bark_url or '', 'bark_title': settings.bark_title or '', 'bark_body': settings.bark_body or ''},
        'items': [{'id': i.id, 'name': i.name} for i in items],
        'records': [{'id': r.id, 'date': r.date, 'next_time': r.next_time, 'group': list(r.data.keys())[0] if r.data else '', 'quantity': list(r.data.values())[0] if r.data else 0} for r in records]
    })

@app.route('/api/record', methods=['POST'])
@login_required
def add_record():
    data = request.json; settings = Settings.query.first(); date_str = data.get('date').replace('T', ' ')
    dt_obj = datetime.strptime(date_str, '%Y-%m-%d %H:%M'); next_dt = dt_obj + timedelta(hours=settings.interval_hours)
    new_record = Record(date=date_str, next_time=next_dt.strftime('%Y-%m-%d %H:%M'), data={data.get('group'): data.get('quantity')}, notified=False)
    db.session.add(new_record); db.session.commit()
    return jsonify({'success': True})

@app.route('/api/record/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def modify_record_api(id):
    record = Record.query.get_or_404(id)
    if request.method == 'DELETE': db.session.delete(record)
    elif request.method == 'PUT':
        data = request.json; settings = Settings.query.first(); new_date = data.get('date').replace('T', ' ')
        record.date = new_date; record.next_time = (datetime.strptime(new_date, '%Y-%m-%d %H:%M') + timedelta(hours=settings.interval_hours)).strftime('%Y-%m-%d %H:%M'); record.notified = False
        old_key = list(record.data.keys())[0]; record.data = {old_key: data.get('value')}
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/settings', methods=['POST'])
@login_required
def save_settings():
    data = request.json; action = data.get('action'); settings = Settings.query.first()
    if action == 'add_item':
        name = data.get('name')
        if name and not Item.query.filter_by(name=name).first(): db.session.add(Item(name=name))
    elif action == 'edit_item':
        item = Item.query.get(data.get('id')); new_name = data.get('name')
        if item and new_name and item.name != new_name and not Item.query.filter_by(name=new_name).first():
            old_name = item.name; item.name = new_name
            for r in Record.query.all():
                if r.data and old_name in r.data:
                    new_data = dict(r.data); new_data[new_name] = new_data.pop(old_name); r.data = new_data
    elif action == 'delete_item':
        item = Item.query.get(data.get('id'))
        if item: db.session.delete(item)
    elif action == 'update_interval': settings.interval_hours = int(data.get('interval_hours'))
    elif action == 'update_bark':
        settings.bark_url = data.get('bark_url'); settings.bark_title = data.get('bark_title'); settings.bark_body = data.get('bark_body')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/test_bark', methods=['POST'])
@login_required
def test_bark_api():
    data = request.json; url = data.get('bark_url')
    if not url: return jsonify({'error': '请先填写 Bark URL'}), 400
    title = data.get('bark_title', '测试').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    body = data.get('bark_body', '成功连通！').replace("{group}", "通道测试").replace("{time}", datetime.now().strftime('%H:%M'))
    api_url = f"{url.rstrip('/')}/{title}/{body}?sound=minuet&group=MatrixPilot&isArchive=1"
    try:
        r = requests.get(api_url, timeout=5)
        if r.status_code == 200: return jsonify({'msg': '测试推送成功，请查看手机！'})
        return jsonify({'error': f'接口拒绝请求 (HTTP {r.status_code})'}), 400
    except Exception as e: return jsonify({'error': str(e)}), 400

@app.route('/api/nodes')
@login_required
def get_nodes():
    conn = get_lp_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM devices ORDER BY first_seen ASC"); rows = c.fetchall()
    nodes = []; now = time.time()
    for r in rows: nodes.append({ "device_id": r['device_id'], "nickname": r['nickname'], "is_online": (now - r['last_seen']) < 15, "process_running": bool(r['process_running']), "has_password": bool(r['password']), "template_id": r['template_id'] })
    conn.close()
    return jsonify({"nodes": nodes})

@app.route('/api/node/delete', methods=['POST'])
@login_required
def delete_node():
    device_id = request.json.get('device_id')
    if not device_id: return jsonify({"status": "error"}), 400
    conn = get_lp_db_connection()
    try: conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,)); conn.commit(); return jsonify({"status": "success"})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

@app.route('/api/reset_round', methods=['POST'])
@login_required
def reset_round():
    device_id = request.json.get('device_id')
    if not device_id: return jsonify({"status": "error"}), 400
    conn = get_lp_db_connection(); c = conn.cursor()
    c.execute("SELECT template_id FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone(); conn.close()
    template_id = row['template_id'] if row and row['template_id'] else 'default'
    key = f"{device_id}_{template_id}"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    round_start_times[key] = now_str; save_round_times(round_start_times)
    return jsonify({"status": "success", "round_start_time": now_str})

@app.route('/api/templates', methods=['GET'])
@login_required
def get_templates():
    tpls = [{"id": k, "name": v["name"]} for k, v in LOG_PARSERS.items()]
    return jsonify({"templates": tpls})

@app.route('/api/set_template', methods=['POST'])
@login_required
def set_template():
    data = request.json
    device_id = data.get('node_id'); template_id = data.get('template_id')
    if not device_id or not template_id: return jsonify({"error": "Missing params"}), 400
    conn = get_lp_db_connection()
    conn.execute("UPDATE devices SET template_id = ?, last_msg = '正常' WHERE device_id = ?", (template_id, device_id))
    conn.commit(); conn.close()
    return jsonify({"status": "success"})

@app.route('/api/history_logs')
@login_required
def get_history_logs():
    target_node_id = request.args.get('node_id'); target_date = request.args.get('date') 
    if not target_node_id or not target_date: return jsonify({"logs": []})
    conn = get_lp_db_connection(); c = conn.cursor()
    try:
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (target_node_id,))
        row = c.fetchone(); template_id = row['template_id'] if row and row['template_id'] else 'default'
        target_date_slash = target_date.replace('-', '/')
        c.execute("SELECT log_time, nickname, item_type, quantity FROM logs WHERE device_id = ? AND template_id = ? AND (log_time LIKE ? OR log_time LIKE ?) ORDER BY id DESC", 
                  (target_node_id, template_id, f"{target_date}%", f"{target_date_slash}%"))
        return jsonify({"logs": [dict(row) for row in c.fetchall()]})
    except: return jsonify({"logs": []})
    finally: conn.close()

@app.route('/api/update_history', methods=['POST'])
@login_required
def update_history():
    data = request.json; device_id = data.get('device_id'); conn = get_lp_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (device_id,))
        row = c.fetchone(); template_id = row['template_id'] if row and row['template_id'] else 'default'
        c.execute("REPLACE INTO daily_overrides (date, device_id, template_id, manual_users, manual_sum) VALUES (?, ?, ?, ?, ?)", (data.get('date'), device_id, template_id, data.get('manual_users'), data.get('manual_sum')))
        conn.commit(); return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/api/user_total', methods=['GET'])
@login_required
def get_user_total():
    target_node_id = request.args.get('node_id'); nickname = request.args.get('nickname', '')
    start_date = request.args.get('start_date', ''); end_date = request.args.get('end_date', ''); calc_all = request.args.get('calc_all', '0')
    if not target_node_id: return jsonify({"error": "Missing node_id"}), 400
    conn = get_lp_db_connection(); c = conn.cursor()
    try:
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (target_node_id,))
        row = c.fetchone(); template_id = row['template_id'] if row and row['template_id'] else 'default'
        start_dt = datetime.min; end_dt = datetime.max
        if start_date:
            start_str = start_date.replace('T', ' ')
            if len(start_str) == 10: start_str += " 00:00:00"
            elif len(start_str) == 16: start_str += ":00"
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        if end_date:
            end_str = end_date.replace('T', ' ')
            if len(end_str) == 10: end_str += " 23:59:59"
            elif len(end_str) == 16: end_str += ":59"
            end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")

        if calc_all == '1':
            c.execute("SELECT log_time, quantity FROM logs WHERE device_id = ? AND template_id = ?", (target_node_id, template_id))
            rows = c.fetchall(); total = 0
            for r in rows:
                log_dt = parse_log_date(r['log_time'])
                if log_dt and start_dt <= log_dt <= end_dt: total += r['quantity']
            return jsonify({"total": total})
        elif not nickname:
            c.execute("SELECT DISTINCT nickname FROM logs WHERE device_id = ? AND template_id = ?", (target_node_id, template_id))
            return jsonify({"users": [r['nickname'] for r in c.fetchall() if r['nickname']]})
        else:
            if start_date or end_date:
                c.execute("SELECT log_time, quantity FROM logs WHERE device_id = ? AND nickname = ? AND template_id = ?", (target_node_id, nickname, template_id))
                rows = c.fetchall(); total = 0
                for r in rows:
                    log_dt = parse_log_date(r['log_time'])
                    if log_dt and start_dt <= log_dt <= end_dt: total += r['quantity']
                return jsonify({"total": total})
            else:
                c.execute("SELECT SUM(quantity) as total FROM logs WHERE device_id = ? AND nickname = ? AND template_id = ?", (target_node_id, nickname, template_id))
                r = c.fetchone()
                return jsonify({"total": r['total'] if r['total'] else 0})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

@app.route('/api/stats')
@login_required
def get_stats():
    target_node_id = request.args.get('node_id'); req_password = request.args.get('password', '')
    conn = get_lp_db_connection(); c = conn.cursor()
    try:
        process_status_text = "未连接"; current_template = "default"; detected_template = ""
        if target_node_id:
            try:
                c.execute("SELECT last_seen, process_running, password, template_id, last_msg, detected_template FROM devices WHERE device_id = ?", (target_node_id,))
                row = c.fetchone()
                if row:
                    if row['password'] and row['password'] != req_password:
                        conn.close(); return jsonify({"error": "auth_failed"}), 403
                    current_template = row['template_id']; detected_template = row['detected_template'] if row['detected_template'] else ""
                    if row['last_msg'] == "模板错误": process_status_text = "模板错误"
                    else:
                        if (time.time() - row['last_seen']) >= 15: process_status_text = "离线" 
                        elif row['process_running']: process_status_text = "运行中"
                        else: process_status_text = "未运行"
                else: process_status_text = "未知设备"
            except sqlite3.OperationalError: process_status_text = "数据异常"
        else: process_status_text = "请选择节点"

        query = "SELECT id, log_time, nickname, quantity, item_type FROM logs WHERE device_id = ? AND template_id = ?"
        c.execute(query, (target_node_id, current_template))
        all_raw_logs = [dict(row) for row in c.fetchall()]

        now = datetime.now()
        base_cutoff = now - timedelta(hours=48)
        key = f"{target_node_id}_{current_template}"
        reset_time_obj = None
        
        if key in round_start_times:
            try: reset_time_obj = datetime.strptime(round_start_times[key], '%Y-%m-%d %H:%M:%S')
            except: pass
        elif target_node_id in round_start_times:
            try: reset_time_obj = datetime.strptime(round_start_times[target_node_id], '%Y-%m-%d %H:%M:%S')
            except: pass

        if reset_time_obj:
            cutoff_time = max(base_cutoff, reset_time_obj)
            date_range_str = f"{reset_time_obj.strftime('%m-%d %H:%M')} - 至今"
        else:
            cutoff_time = base_cutoff
            date_range_str = "未重置 (近48小时)"

        overview_logs = []
        for log in all_raw_logs:
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time: 
                overview_logs.append({ "nickname": log['nickname'], "quantity": log['quantity'], "item_type": log['item_type'], "log_dt": log_dt })

        total_users = len(set(l['nickname'] for l in overview_logs))
        total_wins = sum(l['quantity'] for l in overview_logs if l.get('item_type') == '钻石')
        total_physical_wins = sum(l['quantity'] for l in overview_logs if l.get('item_type') != '钻石')
        
        rank_map = {}
        for l in overview_logs:
            if l['nickname'] not in rank_map: rank_map[l['nickname']] = {"win_times": 0, "win_sum": 0}
            rank_map[l['nickname']]["win_times"] += 1; rank_map[l['nickname']]["win_sum"] += l['quantity']
        
        rank_list = [{"nickname": k, "win_times": v["win_times"], "win_sum": v["win_sum"]} for k, v in rank_map.items()]
        rank_list.sort(key=lambda x: x['win_sum'], reverse=True)

        if not overview_logs: date_range_str = "暂无数据"

        query_det = "SELECT id, log_time, nickname, item_type, quantity FROM logs WHERE device_id = ? AND template_id = ? ORDER BY id DESC LIMIT 5000"
        c.execute(query_det, (target_node_id, current_template))
        details = [log for log in [dict(row) for row in c.fetchall()] if parse_log_date(log['log_time']) and parse_log_date(log['log_time']) >= cutoff_time]

        hist_sql = '''SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum 
                      FROM logs l 
                      LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date AND d.device_id = l.device_id AND d.template_id = l.template_id
                      WHERE l.device_id = ? AND l.template_id = ?
                      GROUP BY date_str'''
        c.execute(hist_sql, (target_node_id, current_template))
        
        history_list = []
        today_strs = (now.strftime('%Y-%m-%d'), now.strftime('%Y/%m/%d'), now.strftime('%Y.%m.%d'))
        for row in c.fetchall():
            if row['date_str'] in today_strs: continue
            final_users = row['manual_users'] if row['manual_users'] is not None else row['calc_users']
            final_sum = row['manual_sum'] if row['manual_sum'] is not None else row['calc_sum']
            dt = parse_log_date(row['date_str'] + " 00:00:00")
            history_list.append({ "date": row['date_str'], "user_count": final_users, "daily_sum": final_sum, "is_manual": row['manual_users'] is not None, "sort_key": dt if dt else datetime.min })
        history_list.sort(key=lambda x: x['sort_key'], reverse=True)

    except Exception as e:
        print(f"Stats Error: {e}", flush=True)
        process_status_text, total_users, total_wins, total_physical_wins, rank_list, details, history_list = "Error", 0, 0, 0, [], [], []
        current_template, detected_template = "default", ""
        date_range_str = "Error"
    
    conn.close()
    return jsonify({
        "process_status": process_status_text, "current_template": current_template, "detected_template": detected_template,
        "total_users": total_users, "total_wins": total_wins, "total_physical_wins": total_physical_wins, 
        "rank_list": rank_list, "date_range": date_range_str, "details": details, "history_data": history_list
    })

@app.route('/api/health', methods=['GET'])
def health_check(): return jsonify({"status": "online", "server": "Matrix & Little Pilot Unified"})

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json; device_id = data.get('device_id'); nickname = data.get('nickname', 'Unknown'); password = data.get('password', '')
    process_running = 1 if data.get('process_running', False) else 0
    if not device_id: return jsonify({"status": "error"}), 400
    try:
        update_device_status(device_id, nickname, process_running, password)
        conn = get_lp_db_connection(); c = conn.cursor()
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (device_id,))
        row = c.fetchone(); conn.close(); template_id = row['template_id'] if row and row['template_id'] else 'default'
        parser = LOG_PARSERS.get(template_id, LOG_PARSERS['default'])
        return jsonify({ "status": "ok", "file_rule": parser.get("file_rule", "lot.txt"), "folder_rule": parser.get("folder_rule", "") })
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    sys.stdout.flush()
    file = request.files.get('file'); device_id = request.form.get('device_id')
    nickname = request.form.get('nickname', 'Unknown'); password = request.form.get('password', '')
    process_running = 1 if request.form.get('process_running', 'False') == 'True' else 0
    client_template = request.form.get('template_id', 'default')
    
    if not file or not device_id: return jsonify({"status": "error"}), 400
    update_device_status(device_id, nickname, process_running, password)
    
    conn = get_lp_db_connection(); c = conn.cursor()
    parser = LOG_PARSERS.get(client_template, LOG_PARSERS['default'])
    pattern = parser['pattern']
    
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    lines = content.split('\n')
    
    new_count = 0; total_matched = 0
    for line in lines:
        line = line.strip()
        if not line: continue 
        match = re.search(pattern, line)
        if match:
            total_matched += 1
            if client_template == 'pixiu':
                log_time_raw, nick, raw_val = match.groups()
                log_time = log_time_raw.replace('年', '-').replace('月', '-').replace('日', '').replace('时', ':').replace('分', ':').replace('秒', '')
                if '钻' in raw_val:
                    q_match = re.search(r'\d+', raw_val)
                    quantity = int(q_match.group()) if q_match else 1; final_item_type = "钻石"
                else: final_item_type = raw_val; quantity = 1
            else:
                log_time, nick, q_str = match.group(1), match.group(2), match.group(3)
                quantity = int(q_str); final_item_type = parser['item_type']
            
            unique_sign = f"{log_time}_{nick}_{final_item_type}_{quantity}_{device_id}" 
            try:
                c.execute("INSERT INTO logs (log_time, nickname, item_type, quantity, unique_sign, device_id, template_id) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                          (log_time, nick, final_item_type, quantity, unique_sign, device_id, client_template))
                new_count += 1
            except sqlite3.IntegrityError: pass 
            
    last_msg = "正常"
    if len(lines) > 10 and total_matched == 0: last_msg = "模板错误"
    c.execute("UPDATE devices SET last_msg = ?, detected_template = ? WHERE device_id = ?", (last_msg, client_template, device_id))
    conn.commit(); conn.close()
    return jsonify({"status": "success", "new_entries": new_count})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)