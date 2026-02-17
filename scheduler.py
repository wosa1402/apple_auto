import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from worker import run_task

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self, db, ocr_instance, lang_text, data_dir="data"):
        self.db = db
        self.ocr = ocr_instance
        self.lang = lang_text
        self.data_dir = data_dir
        self.scheduler = BackgroundScheduler(daemon=True)
        self.running = False
        self._lock = threading.Lock()
        self._current_account = None

    def start(self):
        self.scheduler.add_job(
            self._check_and_run,
            "interval",
            seconds=60,
            id="main_checker",
            max_instances=1,
        )
        self.scheduler.start()
        logger.info("Task scheduler started")

    def _check_and_run(self):
        if self.running:
            return

        with self._lock:
            if self.running:
                return
            self.running = True

        try:
            due_accounts = self.db.get_due_accounts()
            if due_accounts:
                logger.info(f"Found {len(due_accounts)} account(s) due for check")
            for account in due_accounts:
                self._current_account = account["username"]
                try:
                    run_task(
                        account_id=account["id"],
                        db=self.db,
                        ocr_instance=self.ocr,
                        lang_text=self.lang,
                        data_dir=self.data_dir,
                    )
                except Exception as e:
                    logger.error(f"Task for account {account['id']} failed: {e}")
                self._current_account = None
        finally:
            with self._lock:
                self.running = False

    def trigger_now(self, account_id):
        """Manually trigger a check for a specific account. Non-blocking."""
        def _run():
            with self._lock:
                if self.running:
                    logger.warning("Scheduler is busy, cannot trigger now")
                    return
                self.running = True
            try:
                account = self.db.get_account(account_id)
                if account:
                    self._current_account = account["username"]
                run_task(
                    account_id=account_id,
                    db=self.db,
                    ocr_instance=self.ocr,
                    lang_text=self.lang,
                    data_dir=self.data_dir,
                )
            except Exception as e:
                logger.error(f"Manual trigger for account {account_id} failed: {e}")
            finally:
                self._current_account = None
                with self._lock:
                    self.running = False

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def get_status(self):
        return {
            "running": self.running,
            "current_account": self._current_account,
        }

    def shutdown(self):
        self.scheduler.shutdown(wait=False)
        logger.info("Task scheduler stopped")
