import logging
import os
from functools import wraps

import ddddocr
import urllib3
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from config import Config as AppConfig
from env_check import check_environment
from lang import en_us, vi_vn, zh_cn
from models import Database
from notifier import send_notification
from scheduler import TaskScheduler

urllib3.disable_warnings()


def create_app():
    app = Flask(__name__)
    cfg = AppConfig()
    app.secret_key = cfg.SECRET_KEY

    # Ensure data directory exists before logging setup
    os.makedirs(cfg.DATA_DIR, exist_ok=True)

    # Logging
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(cfg.DATA_DIR, "app.log"), encoding="utf-8"),
        ],
    )
    logger = logging.getLogger(__name__)

    # Initialize components
    db = Database(cfg.DATABASE_PATH)
    ocr = ddddocr.DdddOcr(show_ad=False)

    # Check WebDriver environment
    webdriver_url_cfg = cfg.WEBDRIVER_URL
    if webdriver_url_cfg and webdriver_url_cfg != "local":
        env_status = {
            "ready": True,
            "message": f"使用远程 WebDriver: {webdriver_url_cfg}",
            "chrome_ok": True,
            "driver_ok": True,
            "chrome_path": None,
            "chromedriver_path": None,
            "auto_installed": False,
        }
        logger.info(env_status["message"])
    else:
        logger.info("正在检测本地 WebDriver 环境...")
        env_status = check_environment()
        if env_status["ready"]:
            logger.info(f"WebDriver 环境就绪: {env_status['message']}")
        else:
            logger.warning(f"WebDriver 环境异常: {env_status['message']}")
    app.config["ENV_STATUS"] = env_status

    lang_map = {"zh_cn": zh_cn, "en_us": en_us, "vi_vn": vi_vn}
    lang_cls = lang_map.get(cfg.LANG, zh_cn)
    lang_text = lang_cls()

    # Seed default settings if empty
    _defaults = {
        "admin_password": cfg.ADMIN_PASSWORD,
        "webdriver_url": cfg.WEBDRIVER_URL,
        "headless": "true" if cfg.HEADLESS else "false",
        "default_check_interval": "30",
    }
    for k, v in _defaults.items():
        if not db.get_setting(k):
            db.set_setting(k, v)

    scheduler = TaskScheduler(db, ocr, lang_text, cfg.DATA_DIR)

    # ── Auth ──

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    # ── Routes ──

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            password = request.form.get("password", "")
            admin_pw = db.get_setting("admin_password", cfg.ADMIN_PASSWORD)
            if password == admin_pw:
                session["authenticated"] = True
                return redirect(url_for("dashboard"))
            flash("密码错误", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("authenticated", None)
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.route("/")
    @login_required
    def dashboard():
        accounts = db.list_accounts()
        sched_status = scheduler.get_status()
        env = app.config.get("ENV_STATUS", {})
        # Also check if user configured remote WebDriver in settings
        wd_url = db.get_setting("webdriver_url", "local")
        if wd_url and wd_url != "local":
            env = dict(env, ready=True, message=f"使用远程 WebDriver: {wd_url}")
        return render_template("dashboard.html", accounts=accounts, scheduler=sched_status, env_status=env)

    # ── Account CRUD ──

    @app.route("/account/add", methods=["GET", "POST"])
    @login_required
    def account_add():
        if request.method == "POST":
            data = _parse_account_form(request.form)
            db.create_account(data)
            flash("账号添加成功", "success")
            return redirect(url_for("dashboard"))
        proxies = db.list_proxies()
        return render_template("account_form.html", account=None, proxies=proxies)

    @app.route("/account/<int:account_id>/edit", methods=["GET", "POST"])
    @login_required
    def account_edit(account_id):
        if request.method == "POST":
            data = _parse_account_form(request.form)
            db.update_account(account_id, data)
            flash("账号更新成功", "success")
            return redirect(url_for("dashboard"))
        account = db.get_account(account_id)
        if not account:
            flash("账号未找到", "danger")
            return redirect(url_for("dashboard"))
        proxies = db.list_proxies()
        return render_template("account_form.html", account=account, proxies=proxies)

    @app.route("/account/<int:account_id>/delete", methods=["POST"])
    @login_required
    def account_delete(account_id):
        db.delete_account(account_id)
        return jsonify({"status": True})

    @app.route("/account/<int:account_id>/toggle", methods=["POST"])
    @login_required
    def account_toggle(account_id):
        db.toggle_account(account_id)
        return jsonify({"status": True})

    @app.route("/account/<int:account_id>/run", methods=["POST"])
    @login_required
    def account_run(account_id):
        if scheduler.running:
            return jsonify({"status": False, "message": "调度器正忙"})
        scheduler.trigger_now(account_id)
        return jsonify({"status": True, "message": "任务已触发"})

    # ── Proxy CRUD ──

    @app.route("/proxies")
    @login_required
    def proxy_list():
        proxies = db.list_proxies()
        blacklist = db.list_blacklist()
        return render_template("proxy_list.html", proxies=proxies, blacklist=blacklist)

    @app.route("/proxy/add", methods=["GET", "POST"])
    @login_required
    def proxy_add():
        if request.method == "POST":
            data = _parse_proxy_form(request.form)
            db.create_proxy(data)
            flash("代理添加成功", "success")
            return redirect(url_for("proxy_list"))
        return render_template("proxy_form.html", proxy=None)

    @app.route("/proxy/<int:proxy_id>/edit", methods=["GET", "POST"])
    @login_required
    def proxy_edit(proxy_id):
        if request.method == "POST":
            data = _parse_proxy_form(request.form)
            db.update_proxy(proxy_id, data)
            flash("代理更新成功", "success")
            return redirect(url_for("proxy_list"))
        proxy = db.get_proxy(proxy_id)
        if not proxy:
            flash("代理未找到", "danger")
            return redirect(url_for("proxy_list"))
        return render_template("proxy_form.html", proxy=proxy)

    @app.route("/proxy/<int:proxy_id>/delete", methods=["POST"])
    @login_required
    def proxy_delete(proxy_id):
        db.delete_proxy(proxy_id)
        return jsonify({"status": True})

    @app.route("/proxy/blacklist/clear", methods=["POST"])
    @login_required
    def proxy_blacklist_clear():
        db.clear_blacklist()
        return jsonify({"status": True})

    # ── Records ──

    @app.route("/records")
    @login_required
    def records():
        page = request.args.get("page", 1, type=int)
        result = db.list_records(page=page, per_page=50)
        return render_template("records.html", records=result)

    # ── Settings ──

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        setting_keys = [
            "admin_password", "tg_bot_token", "tg_chat_id",
            "wx_pusher_id", "webhook_url", "webdriver_url",
            "default_check_interval", "headless", "proxy_pool_url",
        ]
        if request.method == "POST":
            for key in setting_keys:
                value = request.form.get(key, "")
                if key == "admin_password" and not value:
                    continue
                if key == "headless":
                    value = "true" if request.form.get(key) else "false"
                db.set_setting(key, value)
            flash("设置已保存", "success")
            return redirect(url_for("settings"))
        current = {key: db.get_setting(key) for key in setting_keys}
        env = app.config.get("ENV_STATUS", {})
        return render_template("settings.html", settings=current, env_status=env)

    @app.route("/settings/test_notification", methods=["POST"])
    @login_required
    def test_notification():
        s = db.get_all_settings()
        send_notification("测试", "AppleID Auto Lite 通知测试", s)
        return jsonify({"status": True, "message": "测试通知已发送"})

    @app.route("/settings/recheck_env", methods=["POST"])
    @login_required
    def recheck_env():
        wd_url = db.get_setting("webdriver_url", "local")
        if wd_url and wd_url != "local":
            env = {
                "ready": True,
                "message": f"使用远程 WebDriver: {wd_url}",
                "chrome_ok": True,
                "driver_ok": True,
            }
        else:
            env = check_environment()
        app.config["ENV_STATUS"] = env
        return jsonify({"status": True, "env": env})

    # ── API for dashboard auto-refresh ──

    @app.route("/api/status")
    @login_required
    def api_status():
        accounts = db.list_accounts()
        sched_status = scheduler.get_status()
        return jsonify({"accounts": accounts, "scheduler": sched_status})

    # ── Helpers ──

    def _parse_account_form(form):
        return {
            "username": form.get("username", "").strip(),
            "password": form.get("password", "").strip(),
            "remark": form.get("remark", "").strip(),
            "dob": form.get("dob", "").strip(),
            "question1": form.get("question1", "").strip(),
            "answer1": form.get("answer1", "").strip(),
            "question2": form.get("question2", "").strip(),
            "answer2": form.get("answer2", "").strip(),
            "question3": form.get("question3", "").strip(),
            "answer3": form.get("answer3", "").strip(),
            "check_interval": form.get("check_interval", "30"),
            "enable_check_password_correct": 1 if form.get("enable_check_password_correct") else 0,
            "enable_delete_devices": 1 if form.get("enable_delete_devices") else 0,
            "enable_auto_update_password": 1 if form.get("enable_auto_update_password") else 0,
            "fail_retry": 1 if form.get("fail_retry") else 0,
            "proxy_id": form.get("proxy_id") or None,
            "enabled": 1 if form.get("enabled") else 0,
        }

    def _parse_proxy_form(form):
        return {
            "protocol": form.get("protocol", "http"),
            "content": form.get("content", "").strip(),
            "enabled": 1 if form.get("enabled") else 0,
        }

    # Start scheduler
    scheduler.start()

    return app


if __name__ == "__main__":
    cfg = AppConfig()
    app = create_app()
    app.run(host=cfg.HOST, port=cfg.PORT, debug=False)
