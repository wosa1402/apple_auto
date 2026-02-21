import base64
import datetime
import logging
import os
import random
import re
import string
import time
import traceback

from requests import get
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from notifier import send_notification

logger = logging.getLogger(__name__)

PASSWORD_LENGTH = 10


class TaskConfig:
    """Account task configuration built from DB row + global settings."""

    def __init__(self, account_row, settings, proxy_row=None):
        self.username = account_row["username"]
        self.password = account_row["password"] or ""
        # dob stored as YYYY-MM-DD in SQLite; convert to MMDDYYYY for iforgot page
        raw_dob = account_row["dob"]
        try:
            d = datetime.datetime.strptime(raw_dob, "%Y-%m-%d")
            self.dob = d.strftime("%m%d%Y")
        except (ValueError, TypeError):
            self.dob = raw_dob  # fallback: pass through as-is
        self.answer = {
            account_row["question1"]: account_row["answer1"],
            account_row["question2"]: account_row["answer2"],
            account_row["question3"]: account_row["answer3"],
        }
        self.check_interval = int(account_row.get("check_interval", 30))
        self.enable_check_password_correct = bool(account_row.get("enable_check_password_correct", 0))
        self.enable_delete_devices = bool(account_row.get("enable_delete_devices", 0))
        self.enable_auto_update_password = bool(account_row.get("enable_auto_update_password", 0))
        self.fail_retry = bool(account_row.get("fail_retry", 1))
        self.enable = bool(account_row.get("enabled", 1))

        # From global settings
        self.webdriver = settings.get("webdriver_url", "local") or "local"
        self.headless = settings.get("headless", "true").lower() != "false"

        # Notification settings
        self.tg_bot_token = settings.get("tg_bot_token", "")
        self.tg_chat_id = settings.get("tg_chat_id", "")
        self.wx_pusher_id = settings.get("wx_pusher_id", "")
        self.webhook = settings.get("webhook_url", "")

        # Proxy
        self.proxy = ""
        self.proxy_id = -1
        self.proxy_type = ""
        self.proxy_content = ""
        if proxy_row and proxy_row.get("enabled"):
            self.proxy_id = proxy_row["id"]
            self.proxy_type = proxy_row.get("protocol", "")
            self.proxy_content = proxy_row.get("content", "")
            if self.proxy_content and self.proxy_type:
                if "url" in self.proxy_type:
                    try:
                        base_type = self.proxy_type.split("+")[0]
                        resolved = get(self.proxy_content, timeout=10).text.strip()
                        self.proxy = f"{base_type}://{resolved}"
                    except Exception as e:
                        logger.error(f"Failed to resolve proxy from URL: {e}")
                        self.proxy = ""
                elif self.proxy_type in ("socks5", "http"):
                    self.proxy = f"{self.proxy_type}://{self.proxy_content}"
                else:
                    logger.error(f"Invalid proxy type: {self.proxy_type}")
                    self.proxy = ""

    def get_notification_settings(self):
        return {
            "tg_bot_token": self.tg_bot_token,
            "tg_chat_id": self.tg_chat_id,
            "wx_pusher_id": self.wx_pusher_id,
            "webhook_url": self.webhook,
        }


class TaskCallbacks:
    """Bridge between automation and DB/notifications — replaces the old API class."""

    def __init__(self, db, account_id, config, lang_text, data_dir="data"):
        self.db = db
        self.account_id = account_id
        self.config = config
        self.lang = lang_text
        self.data_dir = data_dir
        self.proxy_was_blocked = False

    def update_message(self, username, message):
        try:
            self.db.update_account_message(username, message)
        except Exception as e:
            logger.error(f"Failed to update message: {e}")

    def report_proxy_error(self, proxy_id):
        self.proxy_was_blocked = True
        if proxy_id and proxy_id > 0:
            try:
                self.db.disable_proxy(proxy_id)
            except Exception as e:
                logger.error(f"Failed to disable proxy: {e}")

    def disable_account(self, username):
        try:
            self.db.disable_account(username)
        except Exception as e:
            logger.error(f"Failed to disable account: {e}")

    def notify(self, content):
        proxy = self.config.proxy
        if getattr(self.config, 'proxy_from_pool', False):
            proxy = ""
        send_notification(
            self.config.username,
            content,
            self.config.get_notification_settings(),
            proxy=proxy,
        )

    def record_error(self, driver):
        error_dir = os.path.join(self.data_dir, "errors", str(self.account_id))
        os.makedirs(error_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            with open(os.path.join(error_dir, f"{ts}_error.html"), "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            driver.save_screenshot(os.path.join(error_dir, f"{ts}_error.png"))
            logger.info(f"Error screenshot saved to {error_dir}")
        except Exception:
            logger.error(self.lang.failOnSavingScreenshot)


class AppleIDAutomation:
    """All Selenium automation for Apple ID check/unlock/password-reset.

    Ported from the original ID class with global→self substitution.
    All Selenium selectors and logic preserved verbatim.
    """

    def __init__(self, config, driver, ocr, lang_text, callbacks):
        self.config = config
        self.driver = driver
        self.ocr = ocr
        self.lang = lang_text
        self.callbacks = callbacks
        self.username = config.username
        self.password = config.password
        self.dob = config.dob
        self.answer = config.answer

    def generate_password(self):
        pw = ""
        chars = string.digits * 2 + string.ascii_letters
        while not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)', pw):
            pw = ''.join(random.sample(chars, k=PASSWORD_LENGTH))
        return pw

    def get_answer(self, question):
        for item in self.answer:
            if question.find(item) != -1:
                return self.answer.get(item)
        return ""

    def _find_first(self, locators, timeout=5, clickable=False):
        last_error = None
        condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
        for by, selector in locators:
            try:
                return WebDriverWait(self.driver, timeout).until(condition((by, selector)))
            except BaseException as e:
                last_error = e
        if last_error is not None:
            raise last_error
        raise RuntimeError("No valid locator provided")

    def _find_all_first(self, locators, timeout=5, min_count=1):
        for by, selector in locators:
            try:
                elements = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_all_elements_located((by, selector))
                )
                visible_elements = []
                for element in elements:
                    try:
                        if element.is_displayed():
                            visible_elements.append(element)
                    except BaseException:
                        continue
                if len(visible_elements) >= min_count:
                    return visible_elements
            except BaseException:
                continue
        return []

    def _click_first(self, locators, timeout=5):
        try:
            self._find_first(locators, timeout=timeout, clickable=True).click()
            return True
        except BaseException:
            return False

    def _find_dob_input(self, timeout=5):
        locators = [
            (By.CSS_SELECTOR, "masked-date#birthDate input"),
            (By.CSS_SELECTOR, "#birthDate input.date-input"),
            (By.CSS_SELECTOR, "masked-date input"),
            (By.XPATH, "//masked-date//input"),
            (By.XPATH, "//form-fragment-birthday//input"),
            (By.CSS_SELECTOR, "input.date-input.form-textbox-input"),
            (By.CLASS_NAME, "date-input"),
        ]
        try:
            return self._find_first(locators, timeout=timeout, clickable=True)
        except BaseException:
            return None

    def _click_action_button(self, timeout=5):
        return self._click_first([(By.ID, "action")], timeout=timeout)

    def _click_reset_password_option(self, timeout=5):
        locators = [
            (By.ID, "recoveryOption0"),
            (By.CSS_SELECTOR, "input[name='recoveryOption'][value='reset_password']"),
            (By.CSS_SELECTOR, "label[for='recoveryOption0']"),
            (By.ID, "optionquestions"),
            (By.CSS_SELECTOR, "input[name='device'][value='questions']"),
            (By.CSS_SELECTOR, "label[for='optionquestions']"),
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/sa/idms-flow/div/section/div/web-reset-options/div[2]/div[1]/button"),
            (By.CLASS_NAME, "pwdChange"),
        ]
        return self._click_first(locators, timeout=timeout)

    def _has_any(self, locators):
        """Check if any locator matches a VISIBLE element on the page."""
        for by, selector in locators:
            try:
                elements = self.driver.find_elements(by, selector)
                for el in elements:
                    if el.is_displayed():
                        return True
            except BaseException:
                continue
        return False

    def _wait_for_page_transition(self, page_check_fn, timeout=8):
        """Wait for a page to transition away (its elements become hidden/removed)."""
        try:
            WebDriverWait(self.driver, timeout).until(lambda d: not page_check_fn())
            return True
        except BaseException:
            return False

    def _is_recovery_options_page(self):
        return self._has_any([
            (By.CSS_SELECTOR, "recovery-options"),
            (By.ID, "recoveryOption0"),
            (By.CSS_SELECTOR, "input[name='recoveryOption']"),
        ])

    def _is_authentication_method_page(self):
        return self._has_any([
            (By.CSS_SELECTOR, "authentication-method"),
            (By.ID, "optionquestions"),
            (By.CSS_SELECTOR, "input[name='device']"),
        ])

    def _is_security_questions_page(self):
        return self._has_any([
            (By.CSS_SELECTOR, "verify-security-questions"),
            (By.CSS_SELECTOR, "label.question"),
        ])

    def _is_reset_password_page(self):
        return self._has_any([
            (By.CSS_SELECTOR, "reset-password"),
            (By.CSS_SELECTOR, "web-password-input"),
            (By.ID, "password"),
        ])

    def _is_reset_options_page(self):
        return self._has_any([
            (By.CSS_SELECTOR, "web-reset-options"),
            (By.CLASS_NAME, "pwdChange"),
        ])

    def _advance_unlock_flow_step(self):
        if self._is_recovery_options_page():
            logger.info("解锁流程: 检测到恢复选项页面")
            if not self._click_reset_password_option(timeout=5):
                return "fail"
            if not self._click_action_button(timeout=5):
                return "fail"
            # Wait for recovery options page to transition away
            if not self._wait_for_page_transition(self._is_recovery_options_page, timeout=10):
                logger.warning("解锁流程: 恢复选项页面未完成跳转，等待后重试")
                time.sleep(2)
            return "continue"

        if self._is_authentication_method_page():
            logger.info("解锁流程: 检测到身份验证方式页面")
            if not self._click_first([
                (By.ID, "optionquestions"),
                (By.CSS_SELECTOR, "input[name='device'][value='questions']"),
                (By.CSS_SELECTOR, "label[for='optionquestions']"),
                (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/sa/idms-flow/div/section/div/authentication-method/div[2]/div[2]/label/span"),
                (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/sa/idms-flow/div/main/div/authentication-method/div[2]/div[2]/label/span"),
            ], timeout=5):
                return "fail"
            if not self._click_action_button(timeout=5):
                return "fail"
            # Wait for authentication method page to transition away
            if not self._wait_for_page_transition(self._is_authentication_method_page, timeout=10):
                logger.warning("解锁流程: 身份验证方式页面未完成跳转，等待后重试")
                time.sleep(2)
            return "continue"

        if self._find_dob_input(timeout=1) is not None:
            logger.info("解锁流程: 检测到出生日期页面")
            return "continue" if self.process_dob() else "fail"

        if self._is_security_questions_page():
            logger.info("解锁流程: 检测到安全问题页面")
            return "continue" if self.process_security_question() else "fail"

        if self._is_reset_options_page():
            logger.info("解锁流程: 检测到重置选项页面")
            if not self._click_reset_password_option(timeout=5):
                return "fail"
            if not self._click_action_button(timeout=3):
                pass
            # Wait for reset options page to transition away
            self._wait_for_page_transition(self._is_reset_options_page, timeout=8)
            return "continue"

        if self._is_reset_password_page():
            return "done" if self.process_password() else "fail"

        return "unknown"

    def _run_password_reset_flow(self, max_steps=12):
        last_page = None
        repeat_count = 0
        for step in range(max_steps):
            # Detect current page type for stale detection
            current_page = None
            if self._is_recovery_options_page():
                current_page = "recovery_options"
            elif self._is_authentication_method_page():
                current_page = "authentication_method"
            elif self._find_dob_input(timeout=0) is not None:
                current_page = "dob"
            elif self._is_security_questions_page():
                current_page = "security_questions"
            elif self._is_reset_options_page():
                current_page = "reset_options"
            elif self._is_reset_password_page():
                current_page = "reset_password"

            # If we're seeing the same page again, the transition didn't complete
            if current_page and current_page == last_page:
                repeat_count += 1
                if repeat_count >= 3:
                    logger.warning(f"解锁流程: 页面 {current_page} 连续重复 {repeat_count} 次，放弃")
                    return False
                logger.info(f"解锁流程: 页面 {current_page} 仍在显示，等待跳转... (第{repeat_count}次)")
                time.sleep(3)
                continue
            else:
                repeat_count = 0
                last_page = current_page

            step_state = self._advance_unlock_flow_step()
            if step_state == "done":
                return True
            if step_state == "continue":
                time.sleep(2)
                continue
            if step_state == "fail":
                return False

            if self._click_action_button(timeout=2):
                time.sleep(2)
                continue
            if self.check():
                return True
            return False
        return False

    def refresh(self):
        # Retry page load up to 2 times for intermittent failures
        loaded = False
        for attempt in range(2):
            try:
                self.driver.get("https://iforgot.apple.com/password/verify/appleid?language=en_US")
                try:
                    self.driver.switch_to.alert.accept()
                except BaseException:
                    pass
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "iforgot-apple-id")))
                loaded = True
                break
            except BaseException:
                if attempt == 0:
                    logger.warning("iforgot 页面加载失败，正在重试...")
                    time.sleep(3)
        if not loaded:
            logger.error(self.lang.failOnRefreshingPage)
            if self.config.proxy != "":
                logger.error(self.lang.proxyEnabledRefreshing)
                self.callbacks.update_message(self.username, self.lang.proxyEnabledRefreshingAPI)
                self.callbacks.report_proxy_error(self.config.proxy_id)
                self.callbacks.notify(self.lang.proxyEnabledRefreshingAPI)
            else:
                self.callbacks.update_message(self.username, self.lang.failOnLoadingPage)
                self.callbacks.notify(self.lang.failOnLoadingPage)
            self.callbacks.record_error(self.driver)
            return False
        try:
            text = self.driver.find_element(By.XPATH, "/html/body/center[1]/h1").text
        except BaseException:
            return True
        else:
            logger.error(self.lang.IPBlocked)
            logger.error(text)
            self.callbacks.update_message(self.username, self.lang.seeLog)
            if self.config.proxy != "":
                self.callbacks.report_proxy_error(self.config.proxy_id)
            self.callbacks.notify(self.lang.seeLog)
            return False

    def process_verify(self):
        try:
            img_element = self._find_first([
                (By.CSS_SELECTOR, "img[alt='Image challenge']"),
                (By.CSS_SELECTOR, ".idms-captcha-wrapper img"),
                (By.TAG_NAME, "img"),
            ], timeout=10)
            img_src = img_element.get_attribute("src").strip()
            img = img_src.split(",", 1)[-1]
            img_bytes = base64.b64decode(img)
            code = self.ocr.classification(img_bytes)
            captcha_element = self._find_first([
                (By.CSS_SELECTOR, "input.captcha-input"),
                (By.CLASS_NAME, "captcha-input"),
            ], timeout=5, clickable=True)
            captcha_element.clear()
            for char in code:
                captcha_element.send_keys(char)
        except BaseException as e:
            logger.error(self.lang.failOnGettingCaptcha)
            logger.error(e)
            self.callbacks.record_error(self.driver)
            return False
        else:
            return True

    def login(self):
        if not self.refresh():
            return False
        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CLASS_NAME, "iforgot-apple-id")))
            time.sleep(1)
            input_element = self.driver.find_element(By.CLASS_NAME, "iforgot-apple-id")
            for char in self.username:
                input_element.send_keys(char)
        except BaseException:
            logger.error(self.lang.failOnRetrievingPage)
            if self.config.proxy != "":
                logger.error(self.lang.proxyEnabledRefreshing)
                self.callbacks.update_message(self.username, self.lang.proxyEnabledGettingContent)
                self.callbacks.report_proxy_error(self.config.proxy_id)
                self.callbacks.notify(self.lang.proxyEnabledGettingContent)
            else:
                self.callbacks.update_message(self.username, self.lang.failOnGettingPage)
                self.callbacks.notify(self.lang.failOnGettingPage)
            self.callbacks.record_error(self.driver)
            return False
        while True:
            if not self.process_verify():
                return False
            time.sleep(1)
            WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CLASS_NAME, "button-primary"))).click()
            try:
                WebDriverWait(self.driver, 12).until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "masked-date input")
                    or d.find_elements(By.CSS_SELECTOR, "recovery-options")
                    or d.find_elements(By.CSS_SELECTOR, "authentication-method")
                    or d.find_elements(By.CSS_SELECTOR, "verify-security-questions")
                    or d.find_elements(By.CSS_SELECTOR, "reset-password")
                    or d.find_elements(By.CSS_SELECTOR, "input.captcha-input[aria-invalid='true']")
                    or d.find_elements(By.CSS_SELECTOR, "input.iforgot-apple-id[aria-invalid='true']")
                    or d.find_elements(By.CSS_SELECTOR, "idms-textbox[wrapper-class*='captcha-input'] idms-error")
                    or d.find_elements(By.CSS_SELECTOR, "idms-textbox[wrapper-class*='iforgot-apple-id'] idms-error")
                )
            except BaseException:
                logger.error(self.lang.failOnLoadingPage)
                return False

            if self.driver.find_elements(By.CSS_SELECTOR, "input.captcha-input[aria-invalid='true']") \
                    or self.driver.find_elements(By.CSS_SELECTOR, "idms-textbox[wrapper-class*='captcha-input'] idms-error"):
                logger.info(self.lang.captchaFail)
                continue

            appleid_err = ""
            if self.driver.find_elements(By.CSS_SELECTOR, "input.iforgot-apple-id[aria-invalid='true']") \
                    or self.driver.find_elements(By.CSS_SELECTOR, "idms-textbox[wrapper-class*='iforgot-apple-id'] idms-error"):
                try:
                    appleid_err = self.driver.find_element(
                        By.CSS_SELECTOR,
                        "idms-textbox[wrapper-class*='iforgot-apple-id'] idms-error",
                    ).text.strip()
                except BaseException:
                    appleid_err = ""

                msg = appleid_err or ""
                if "not active" in msg:
                    logger.error(self.lang.accountNotActive)
                    self.callbacks.update_message(self.username, self.lang.accountNotActive)
                    self.callbacks.disable_account(self.username)
                    self.callbacks.notify(self.lang.accountNotActive)
                elif "not valid" in msg:
                    logger.error(self.lang.accountNotValid)
                    self.callbacks.update_message(self.username, self.lang.accountNotValid)
                    self.callbacks.disable_account(self.username)
                    self.callbacks.notify(self.lang.accountNotValid)
                elif "Your request could not be completed because of an error" in msg:
                    logger.error(self.lang.blocked)
                    self.callbacks.update_message(self.username, self.lang.blocked)
                    self.callbacks.report_proxy_error(self.config.proxy_id)
                    self.callbacks.notify(self.lang.blocked)
                else:
                    logger.error(f"{self.lang.unknownError}: {msg}")
                    self.callbacks.update_message(self.username, self.lang.unknownError)
                    self.callbacks.notify(self.lang.unknownError)
                self.callbacks.record_error(self.driver)
                return False

            logger.info(self.lang.captchaCorrect)
            break

        logger.info(self.lang.login)
        return True

    def check(self):
        if self._is_recovery_options_page() \
                or self._is_authentication_method_page() \
                or self._is_security_questions_page() \
                or self._is_reset_options_page() \
                or self._is_reset_password_page():
            logger.info(self.lang.locked)
            return False
        if self._find_dob_input(timeout=2) is not None:
            logger.info(self.lang.locked)
            return False
        logger.info(self.lang.notLocked)
        return True

    def check_2fa(self):
        locators = [
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/hsa-two-v2/recovery-web-app/idms-flow/div/div/trusted-phone-number/div/h1"),
            (By.ID, "phoneNumber"),
        ]
        try:
            self._find_first(locators, timeout=3)
        except BaseException:
            logger.info(self.lang.twoStepnotEnabled)
            return False
        else:
            logger.info(self.lang.twoStepEnabled)
            return True

    def unlock_2fa(self):
        unenroll_locators = [
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/hsa-two-v2/recovery-web-app/idms-flow/div/div/trusted-phone-number/div/div/div[1]/idms-step/div/div/div/div[2]/div/div/div/button"),
            (By.CLASS_NAME, "unenroll"),
        ]
        if not self._click_first(unenroll_locators, timeout=8):
            logger.error(self.lang.cantFindDisable2FA)
            self.callbacks.update_message(self.username, self.lang.cantFindDisable2FA)
            self.callbacks.notify(self.lang.cantFindDisable2FA)
            return False
        confirm_locators = [
            (By.XPATH, "/html/body/div[5]/div/div/recovery-unenroll-start/div/idms-step/div/div/div/div[3]/idms-toolbar/div/div/div/button[1]"),
            (By.XPATH, "/html/body/div[4]/div/div/recovery-unenroll-start/div/idms-step/div/div/div/div[3]/idms-toolbar/div/div/div/button[1]"),
        ]
        if not self._click_first(confirm_locators, timeout=10):
            logger.error(self.lang.cantFindDisable2FA)
            self.callbacks.update_message(self.username, self.lang.cantFindDisable2FA)
            self.callbacks.notify(self.lang.cantFindDisable2FA)
            return False
        time.sleep(1)
        try:
            msg = WebDriverWait(self.driver, 3).until(
                EC.presence_of_element_located((By.CLASS_NAME, "error-content"))).get_attribute("innerHTML")
        except BaseException:
            pass
        else:
            logger.error(f"{self.lang.rejectedByApple}\n{msg.strip()}")
            self.callbacks.update_message(self.username, self.lang.rejectedByApple)
            self.callbacks.report_proxy_error(self.config.proxy_id)
            self.callbacks.notify(self.lang.rejectedByApple)
            return False
        if self.process_dob():
            if self.process_security_question():
                self._click_first([
                    (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/hsa-two-v2/recovery-web-app/idms-flow/div/div/verify-security-questions/div/div/div/step-challenge-security-questions/idms-step/div/div/div/div[3]/idms-toolbar/div/div/div/button[1]"),
                    (By.ID, "action"),
                    (By.CLASS_NAME, "button-primary"),
                ], timeout=5)
                if self.process_password():
                    return True
        return False

    def unlock(self):
        if self.check():
            return True
        if self._run_password_reset_flow(max_steps=12):
            return True
        logger.error(self.lang.UnlockFail)
        self.callbacks.update_message(self.username, self.lang.UnlockFail)
        self.callbacks.notify(self.lang.UnlockFail)
        self.callbacks.record_error(self.driver)
        return False

    def login_appleid(self):
        logger.info("Start logging in AppleID")

        # Retry page load up to 3 times for intermittent failures
        page_loaded = False
        for attempt in range(3):
            try:
                # Navigate to blank page first to reset renderer state
                if attempt > 0:
                    try:
                        self.driver.get("about:blank")
                        time.sleep(2)
                    except BaseException:
                        pass
                self.driver.get("https://account.apple.com/sign-in")
                page_loaded = True
                break
            except BaseException as e:
                logger.warning(f"Apple ID 登录页面加载失败 (第{attempt + 1}次): {e}")
                if attempt < 2:
                    time.sleep(3)
        if not page_loaded:
            logger.error(self.lang.loginLoadFail)
            self.callbacks.update_message(self.username, self.lang.loginLoadFail)
            self.callbacks.notify(self.lang.loginLoadFail)
            return False
        try:
            self.driver.switch_to.alert.accept()
        except BaseException:
            pass
        try:
            text = self.driver.find_element(By.XPATH, "/html/body/center[1]/h1").text
        except BaseException:
            pass
        else:
            logger.error(self.lang.IPBlocked)
            logger.error(text)
            self.callbacks.update_message(self.username, self.lang.seeLog)
            if self.config.proxy != "":
                self.callbacks.report_proxy_error(self.config.proxy_id)
            self.callbacks.notify(self.lang.seeLog)
            self.callbacks.record_error(self.driver)
            return False
        try:
            iframe = self._find_first([
                (By.ID, "aid-auth-widget-iFrame"),
                (By.TAG_NAME, "iframe"),
            ], timeout=30)
            self.driver.switch_to.frame(iframe)
        except BaseException:
            # Retry once: reload page and try again
            logger.warning("Apple ID 登录页面 iframe 未找到，重新加载页面重试")
            try:
                self.driver.get("https://account.apple.com/sign-in")
                time.sleep(3)
                iframe = self._find_first([
                    (By.ID, "aid-auth-widget-iFrame"),
                    (By.TAG_NAME, "iframe"),
                ], timeout=30)
                self.driver.switch_to.frame(iframe)
            except BaseException:
                logger.error(self.lang.loginLoadFail)
                self.callbacks.update_message(self.username, self.lang.loginLoadFail)
                self.callbacks.notify(self.lang.loginLoadFail)
                return False
        try:
            WebDriverWait(self.driver, 30).until(EC.element_to_be_clickable((By.ID, "account_name_text_field")))
            input_element = self.driver.find_element(By.ID, "account_name_text_field")
            for char in self.username:
                input_element.send_keys(char)
            input_element.send_keys(Keys.ENTER)
        except BaseException:
            logger.error(self.lang.failOnLoadingPage)
            self.callbacks.update_message(self.username, self.lang.failOnLoadingPage)
            self.callbacks.notify(self.lang.failOnLoadingPage)
            self.callbacks.record_error(self.driver)
            return False
        try:
            if self._click_first([(By.ID, "continue-password")], timeout=2):
                time.sleep(1)
            input_element = self._find_first([
                (By.ID, "password_text_field"),
                (By.CSS_SELECTOR, "input[type='password']"),
            ], timeout=10, clickable=True)
        except BaseException:
            logger.error(self.lang.failOnLoadingPage)
            self.callbacks.update_message(self.username, self.lang.failOnLoadingPage)
            self.callbacks.notify(self.lang.failOnLoadingPage)
            self.callbacks.record_error(self.driver)
            return False
        for char in self.password:
            input_element.send_keys(char)
        time.sleep(1)
        input_element.send_keys(Keys.ENTER)
        time.sleep(5)
        try:
            msg = self.driver.find_element(By.ID, "errMsg").get_attribute("innerHTML")
        except BaseException:
            if not self.config.enable_delete_devices:
                logger.info(self.lang.login)
                return True
        else:
            logger.error(f"{self.lang.LoginFail}\n{msg.strip()}")
            return False
        question_element = self._find_all_first([
            (By.CSS_SELECTOR, "verify-security-questions label"),
            (By.XPATH, "//*[contains(@class, 'question')]"),
        ], timeout=20, min_count=2)
        if len(question_element) < 2:
            self.driver.switch_to.default_content()
            logger.info(self.lang.login)
            return True
        answer0 = self.get_answer(question_element[0].get_attribute("innerHTML"))
        answer1 = self.get_answer(question_element[1].get_attribute("innerHTML"))
        if answer0 == "" or answer1 == "":
            logger.error(self.lang.answerIncorrect)
            self.callbacks.update_message(self.username, self.lang.answerIncorrect)
            self.callbacks.record_error(self.driver)
            return False
        raw_inputs = self._find_all_first([
            (By.CSS_SELECTOR, "verify-security-questions input"),
            (By.XPATH, "//*[contains(@class, 'question')]//input"),
            (By.CSS_SELECTOR, "input.form-textbox-input"),
            (By.CSS_SELECTOR, "input.generic-input-field"),
        ], timeout=10, min_count=2)
        # Filter out password/hidden inputs to avoid filling the wrong field
        answer_inputs = [
            inp for inp in raw_inputs
            if inp.get_attribute("type") not in ("password", "hidden")
        ]
        if len(answer_inputs) < 2:
            logger.error(self.lang.answerIncorrect)
            self.callbacks.update_message(self.username, self.lang.answerIncorrect)
            self.callbacks.record_error(self.driver)
            return False
        for char in answer0:
            answer_inputs[0].send_keys(char)
        time.sleep(1)
        for char in answer1:
            answer_inputs[1].send_keys(char)
        time.sleep(1)
        self._click_first([
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.ID, "action"),
        ], timeout=5)
        time.sleep(5)
        # Check if answers were rejected: .has-errors (iforgot) or questions still visible (account.apple.com)
        answer_error = False
        try:
            self.driver.find_element(By.CLASS_NAME, "has-errors")
            answer_error = True
        except BaseException:
            pass
        if not answer_error:
            still_on_questions = len(self._find_all_first([
                (By.CSS_SELECTOR, "verify-security-questions label"),
                (By.XPATH, "//*[contains(@class, 'question')]"),
            ], timeout=3, min_count=2)) >= 2
            if still_on_questions:
                answer_error = True
        if answer_error:
            logger.error(self.lang.answerNotMatch)
            self.callbacks.update_message(self.username, self.lang.answerNotMatch)
            self.callbacks.notify(self.lang.answerNotMatch)
            self.callbacks.record_error(self.driver)
            return False
        try:
            iframe = self._find_first([
                (By.ID, "aid-auth-widget-iFrame"),
                (By.TAG_NAME, "iframe"),
            ], timeout=10)
            self.driver.switch_to.frame(iframe)
        except BaseException:
            logger.error(self.lang.failOnBypass2FA)
            self.callbacks.record_error(self.driver)
            return False
        try:
            WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH,
                                                                               "/html/body/div[1]/appleid-repair/idms-widget/div/div/div/hsa2-enrollment-flow/div/div/idms-step/div/div/div/div[3]/idms-toolbar/div/div[1]/div/button[2]"))).click()
            self.driver.find_element(By.CLASS_NAME, "nav-cancel").click()
            WebDriverWait(self.driver, 5).until_not(EC.presence_of_element_located((By.CLASS_NAME, "nav-cancel")))
        except BaseException:
            pass
        self.driver.switch_to.default_content()
        logger.info(self.lang.login)
        return True

    def delete_devices(self):
        logger.info(self.lang.startRemoving)
        devices_url = "https://account.apple.com/account/manage/section/devices"
        self.driver.get(devices_url)

        try:
            WebDriverWait(self.driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
            WebDriverWait(self.driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1.page-title")))
        except BaseException:
            logger.error(self.lang.failOnLoadingPage)
            self.callbacks.update_message(self.username, self.lang.failOnLoadingPage)
            self.callbacks.notify(self.lang.failOnLoadingPage)
            self.callbacks.record_error(self.driver)
            return False

        driver = self.driver

        def _safe_click(el):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            except BaseException:
                pass
            try:
                el.click()
                return True
            except BaseException:
                try:
                    driver.execute_script("arguments[0].click();", el)
                    return True
                except BaseException:
                    return False

        def _visible_elements(by, selector):
            try:
                found = driver.find_elements(by, selector)
            except BaseException:
                return []
            visible = []
            for item in found:
                try:
                    if item.is_displayed():
                        visible.append(item)
                except BaseException:
                    continue
            return visible

        def _get_device_buttons():
            for by, selector in [
                (By.CSS_SELECTOR, "div[aria-hidden='false'] button.button-expand"),
                (By.CSS_SELECTOR, "button.button-expand"),
                (By.XPATH, "//button[contains(@class, 'button-expand')]"),
            ]:
                items = _visible_elements(by, selector)
                if items:
                    return items
            return []

        time.sleep(1)
        devices = _get_device_buttons()
        if not devices:
            logger.info(self.lang.noRemoveRequired)
            return True

        logger.info(self.lang.totalDevices(len(devices)))

        idx = 0
        attempts = 0
        max_attempts = max(20, len(devices) * 3)
        while True:
            current = _get_device_buttons()
            if not current:
                break
            if idx >= len(current):
                idx = 0
            if attempts >= max_attempts:
                break

            prev_count = len(current)
            if not _safe_click(current[idx]):
                attempts += 1
                idx += 1
                continue

            modal_locators = [
                (By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay div.modal-dialog[role='dialog']"),
                (By.CSS_SELECTOR, "aside.modal div.modal-dialog[role='dialog']"),
            ]
            modal_loaded = True
            try:
                self._find_first(modal_locators, timeout=12)
            except BaseException:
                modal_loaded = False

            if modal_loaded:
                if not self._click_first([
                    (By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay div.modal-body button.button-secondary"),
                    (By.CSS_SELECTOR, "aside.modal div.modal-body button.button-secondary"),
                    (By.XPATH, "//aside[contains(@class,'modal')]//div[contains(@class,'modal-body')]//button[contains(@class,'button-secondary')]"),
                ], timeout=10):
                    self._click_first([
                        (By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay div.modal-close button[aria-label='Close']"),
                        (By.CSS_SELECTOR, "aside.modal div.modal-close button[aria-label='Close']"),
                    ], timeout=2)
                    attempts += 1
                    idx += 1
                    continue

                if not self._click_first([
                    (By.CSS_SELECTOR, "aside.modal.modal-alert button.button-secondary"),
                    (By.CSS_SELECTOR, "aside.modal-alert button.button-secondary"),
                    (By.XPATH, "//aside[contains(@class,'modal-alert')]//button[contains(@class,'button-secondary')]"),
                ], timeout=12):
                    self._click_first([
                        (By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay div.modal-close button[aria-label='Close']"),
                        (By.CSS_SELECTOR, "aside.modal div.modal-close button[aria-label='Close']"),
                    ], timeout=2)
                    attempts += 1
                    idx += 1
                    continue

                try:
                    WebDriverWait(driver, 20).until_not(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "aside.modal.modal-alert"))
                    )
                except BaseException:
                    pass

                try:
                    WebDriverWait(driver, 20).until_not(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay"))
                    )
                except BaseException:
                    self._click_first([
                        (By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay div.modal-close button[aria-label='Close']"),
                        (By.CSS_SELECTOR, "aside.modal div.modal-close button[aria-label='Close']"),
                    ], timeout=3)
                    try:
                        WebDriverWait(driver, 10).until_not(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "aside.modal.modal-blurry-overlay"))
                        )
                    except BaseException:
                        pass

                removed_ok = False
                try:
                    WebDriverWait(driver, 25).until(lambda d: len(_get_device_buttons()) < prev_count)
                    removed_ok = True
                except BaseException:
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-hidden='false'] h2"))
                        )
                        removed_ok = True
                    except BaseException:
                        removed_ok = False

                time.sleep(1)
                if removed_ok:
                    attempts = 0
                    idx = 0
                else:
                    attempts += 1
                    idx += 1
                continue

            if not self._click_first([
                (By.CLASS_NAME, "button-secondary"),
                (By.XPATH, "//button[contains(@class, 'button-secondary')]"),
            ], timeout=6):
                attempts += 1
                idx += 1
                continue
            self._click_first([
                (By.XPATH, "/html/body/aside[2]/div/div[2]/fieldset/div/div/button[2]"),
                (By.XPATH, "//aside//button[contains(@class, 'button-secondary')]"),
            ], timeout=8)
            time.sleep(1)
            attempts = 0
            idx = 0

        logger.info(self.lang.finishRemoving)
        return True

    def process_dob(self):
        def detect_format_order():
            try:
                masked = self.driver.find_element(By.CSS_SELECTOR, "masked-date#birthDate")
            except BaseException:
                try:
                    masked = self.driver.find_element(By.CSS_SELECTOR, "masked-date")
                except BaseException:
                    return "mdy"
            hint = (masked.get_attribute("format") or masked.get_attribute("focus-placeholder") or "").lower()
            hint = re.sub(r"[^a-z0-9/]", "", hint)
            if "dd/mm" in hint:
                return "dmy"
            if "mm/dd" in hint:
                return "mdy"
            return "mdy"

        def parse_dob(order_hint):
            raw = str(self.dob or "").strip().replace(" ", "")
            if raw == "":
                raise ValueError("dob is empty")

            if re.fullmatch(r"\d{8}", raw):
                first4 = int(raw[:4])
                last4 = int(raw[-4:])
                if 1860 <= first4 <= 2100:
                    y, m, d = first4, int(raw[4:6]), int(raw[6:8])
                elif 1860 <= last4 <= 2100:
                    if order_hint == "dmy":
                        d, m, y = int(raw[:2]), int(raw[2:4]), last4
                    else:
                        m, d, y = int(raw[:2]), int(raw[2:4]), last4
                else:
                    y, m, d = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
                return datetime.date(y, m, d)

            match = re.fullmatch(r"(\d{4})[\\-/.](\d{1,2})[\\-/.](\d{1,2})", raw)
            if match:
                y, mo, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
                return datetime.date(y, mo, d)

            match = re.fullmatch(r"(\d{1,2})[\\-/.](\d{1,2})[\\-/.](\d{4})", raw)
            if match:
                a, b, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
                if order_hint == "dmy":
                    d, mo = a, b
                else:
                    mo, d = a, b
                return datetime.date(y, mo, d)

            raise ValueError("Unsupported dob format")

        def is_action_enabled():
            try:
                btn = self.driver.find_element(By.ID, "action")
            except BaseException:
                return False
            disabled = btn.get_attribute("disabled")
            aria_disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
            if aria_disabled in {"true", "1"}:
                return False
            return disabled is None

        def clear_and_type(el, text):
            el.click()
            try:
                el.send_keys(Keys.CONTROL, "a")
                el.send_keys(Keys.BACKSPACE)
                el.send_keys(Keys.DELETE)
            except BaseException:
                pass
            try:
                el.clear()
            except BaseException:
                pass
            for ch in text:
                el.send_keys(ch)
                time.sleep(0.05)

        try:
            input_box = self._find_dob_input(timeout=8)
            if input_box is None:
                raise RuntimeError("DOB input not found")
            logger.info(f"DOB 输入框已找到，tag={input_box.tag_name}, displayed={input_box.is_displayed()}")
            try:
                WebDriverWait(self.driver, 8).until(EC.presence_of_element_located((By.ID, "action")))
            except BaseException:
                logger.warning("未找到 action 按钮，继续尝试填写 DOB")

            order = detect_format_order()
            dob_date = parse_dob(order)
            logger.info(f"DOB 格式: {order}, 解析结果: {dob_date}")

            if order == "dmy":
                candidates = [
                    f"{dob_date.day:02d}{dob_date.month:02d}{dob_date.year:04d}",
                    f"{dob_date.day:02d}/{dob_date.month:02d}/{dob_date.year:04d}",
                ]
            else:
                candidates = [
                    f"{dob_date.month:02d}{dob_date.day:02d}{dob_date.year:04d}",
                    f"{dob_date.month:02d}/{dob_date.day:02d}/{dob_date.year:04d}",
                ]

            for candidate in candidates:
                logger.info(f"尝试填写 DOB: {candidate}")
                clear_and_type(input_box, candidate)
                try:
                    WebDriverWait(self.driver, 8).until(lambda d: is_action_enabled())
                    logger.info("Continue 按钮已启用")
                except BaseException:
                    logger.warning(f"Continue 按钮未启用，尝试下一个格式")
                    continue

                if not self._click_action_button(timeout=4):
                    try:
                        self.driver.execute_script("document.getElementById('action')?.click?.()")
                    except BaseException:
                        input_box.send_keys(Keys.ENTER)

                try:
                    WebDriverWait(self.driver, 12).until(
                        lambda d: self._is_security_questions_page()
                        or self._is_reset_options_page()
                        or self._is_reset_password_page()
                        or self._is_authentication_method_page()
                        or self._is_recovery_options_page()
                        or len(d.find_elements(By.CSS_SELECTOR, "masked-date input")) == 0
                    )
                except BaseException:
                    continue
                return True

            err = ""
            for sel in [
                "masked-date idms-error",
                "masked-date .form-message",
                ".form-message",
                "idms-error",
            ]:
                try:
                    nodes = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for n in nodes:
                        t = (n.text or "").strip()
                        if t:
                            err = t
                            break
                    if err:
                        break
                except BaseException:
                    continue
            if err:
                logger.error(f"{self.lang.DOB_Error}\n{err}")
            else:
                logger.error(self.lang.DOB_Error)
            self.callbacks.update_message(self.username, self.lang.DOB_Error)
            return False
        except BaseException as e:
            logger.error(f"process_dob 异常: {e}")
            return False

    def process_security_question(self):
        question_element = self._find_all_first([
            (By.CSS_SELECTOR, "verify-security-questions label"),
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/sa/idms-flow/div/section/div/verify-security-questions/div[2]/div/label"),
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/hsa-two-v2/recovery-web-app/idms-flow/div/div/verify-security-questions//label"),
            (By.CLASS_NAME, "question"),
        ], timeout=8, min_count=2)
        if len(question_element) < 2:
            logger.error(self.lang.DOB_Error)
            self.callbacks.update_message(self.username, self.lang.DOB_Error)
            self.callbacks.notify(self.lang.DOB_Error)
            self.callbacks.record_error(self.driver)
            return False
        answer0 = self.get_answer(question_element[0].get_attribute("innerHTML"))
        answer1 = self.get_answer(question_element[1].get_attribute("innerHTML"))
        if answer0 == "" or answer1 == "":
            logger.error(self.lang.answerNotMatch)
            self.callbacks.update_message(self.username, self.lang.answerNotMatch)
            self.callbacks.notify(self.lang.answerNotMatch)
            return False
        answer_inputs = self._find_all_first([
            (By.CSS_SELECTOR, "verify-security-questions input"),
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/sa/idms-flow/div/section/div/verify-security-questions/div[2]/div/idms-textbox/idms-error-wrapper/div/div/input"),
            (By.XPATH, "/html/body/div[1]/iforgot-v2/app-container/div/iforgot-body/hsa-two-v2/recovery-web-app/idms-flow/div/div/verify-security-questions//input"),
            (By.CLASS_NAME, "generic-input-field"),
        ], timeout=8, min_count=2)
        if len(answer_inputs) < 2:
            logger.error(self.lang.answerNotMatch)
            self.callbacks.update_message(self.username, self.lang.answerNotMatch)
            self.callbacks.notify(self.lang.answerNotMatch)
            return False
        for char in answer0:
            answer_inputs[0].send_keys(char)
        time.sleep(1)
        for char in answer1:
            answer_inputs[1].send_keys(char)
        time.sleep(1)
        if not self._click_action_button(timeout=3):
            answer_inputs[1].send_keys(Keys.ENTER)
        try:
            msg = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CLASS_NAME, "form-message"))).get_attribute("innerHTML").strip()
        except BaseException:
            return True
        else:
            logger.error(f"{self.lang.failOnAnswer}\n{msg}")
            self.callbacks.update_message(self.username, self.lang.failOnAnswer)
            self.callbacks.record_error(self.driver)
            return False

    def process_password(self):
        pw_inputs = []
        try:
            locators = [
                (By.CSS_SELECTOR, "reset-password web-password-input input"),
                (By.CSS_SELECTOR, "reset-password confirm-password-input input"),
                (By.CSS_SELECTOR, "idms-password new-password input"),
                (By.CSS_SELECTOR, "idms-password confirm-password-input input"),
                (By.CLASS_NAME, "form-textbox-input"),
            ]
            for by, selector in locators:
                found = self._find_all_first([(by, selector)], timeout=4, min_count=1)
                for item in found:
                    try:
                        if item.is_displayed() and item not in pw_inputs:
                            pw_inputs.append(item)
                    except BaseException:
                        continue
                if len(pw_inputs) >= 2:
                    break
            if len(pw_inputs) < 2:
                raise RuntimeError("Password inputs not found")
        except BaseException:
            logger.error(self.lang.passwordNotFound)
            self.callbacks.update_message(self.username, self.lang.passwordNotFound)
            self.callbacks.notify(self.lang.passwordNotFound)
            self.callbacks.record_error(self.driver)
            return False
        new_password = self.generate_password()
        for item in pw_inputs[:2]:
            try:
                item.clear()
            except BaseException:
                pass
            item.send_keys(new_password)
        time.sleep(1)
        if not self._click_action_button(timeout=3):
            pw_inputs[1].send_keys(Keys.ENTER)
        time.sleep(3)
        self._click_first([
            (By.XPATH, "/html/body/div[5]/div/div/div[1]/idms-step/div/div/div/div[3]/idms-toolbar/div/div/div/button[1]"),
            (By.XPATH, "/html/body/div[4]/div/div/div[1]/idms-step/div/div/div/div[3]/idms-toolbar/div/div/div/button[1]"),
        ], timeout=2)
        try:
            msg = WebDriverWait(self.driver, 3).until(
                EC.presence_of_element_located((By.CLASS_NAME, "error-content"))).get_attribute("innerHTML")
        except BaseException:
            pass
        else:
            logger.error(f"{self.lang.rejectedByApple}: {msg.strip()}")
            self.callbacks.update_message(self.username, self.lang.rejectedByApple)
            self.callbacks.report_proxy_error(self.config.proxy_id)
            self.callbacks.notify(self.lang.rejectedByApple)
            self.callbacks.record_error(self.driver)
            return False
        self.password = new_password
        logger.info(f"{self.lang.passwordUpdated}: {new_password}")
        return True

    def change_password(self):
        if not self.login():
            return False
        logger.info(self.lang.startChangePassword)
        if self._run_password_reset_flow(max_steps=12):
            return True
        logger.error(self.lang.failOnChangePassword)
        self.callbacks.update_message(self.username, self.lang.failOnChangePassword)
        self.callbacks.notify(self.lang.failOnChangePassword)
        self.callbacks.record_error(self.driver)
        return False


# ── Standalone functions ──


def setup_driver(config):
    """Create and return a Chrome WebDriver instance."""
    from env_check import find_chrome, find_chromedriver

    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("enable-automation")
    options.add_argument("--disable-extensions")
    options.add_argument("start-maximized")
    options.add_argument("window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Auto-detect Chrome binary
    chrome_binary = os.environ.get("CHROME_BINARY", "").strip()
    if not chrome_binary:
        chrome_binary = find_chrome() or ""
    if chrome_binary:
        options.binary_location = chrome_binary

    if config.headless:
        options.add_argument("--headless=new")
    if config.proxy:
        options.add_argument(f"--proxy-server={config.proxy}")
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    ]
    options.add_argument(f"user-agent={random.choice(user_agents)}")
    try:
        if config.webdriver != "local":
            driver = webdriver.Remote(command_executor=config.webdriver, options=options)
        else:
            # Auto-detect chromedriver
            chromedriver = os.environ.get("CHROMEDRIVER", "").strip()
            if not chromedriver:
                chromedriver = find_chromedriver() or ""
            if chromedriver:
                from selenium.webdriver.chrome.service import Service as ChromeService
                service = ChromeService(executable_path=chromedriver)
                driver = webdriver.Chrome(service=service, options=options)
            else:
                # Let Selenium Manager handle it (Selenium 4.6+)
                driver = webdriver.Chrome(options=options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    except BaseException as e:
        logger.error(f"WebDriver 启动失败: {e}")
        if config.webdriver == "local" and not chrome_binary:
            logger.error("未检测到 Chrome 浏览器，请安装 Chrome 或在「系统设置」中配置远程 WebDriver URL")
        return None
    else:
        driver.set_page_load_timeout(60)
        return driver


def get_ip(driver):
    """Get current IP address through the browser."""
    try:
        driver.get("https://api.ip.sb/ip")
        ip_address = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.TAG_NAME, "pre"))).text
        logger.info(f"IP: {ip_address}")
        return ip_address
    except BaseException:
        try:
            driver.get("https://myip.ipip.net/s")
            ip_address = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.TAG_NAME, "pre"))).text
            logger.info(f"IP: {ip_address}")
            return ip_address
        except BaseException:
            logger.error("Failed to get IP")
            return ""


def fetch_pool_proxy(pool_url, db, pool_name="auto"):
    """Fetch a non-blacklisted SOCKS5 proxy from ProxyYoPick API.

    Returns (proxy_url, ip) tuple, e.g. ("socks5://1.2.3.4:1080", "1.2.3.4").
    Returns ("", "") on failure.
    """
    try:
        resp = get(f"{pool_url.rstrip('/')}/api/proxies",
                   params={"pool": pool_name}, timeout=10)
        data = resp.json()
        proxies = data.get("proxies") or []
        candidates = [p for p in proxies if not db.is_blacklisted(p["ip"])]
        if not candidates:
            logger.warning("代理池无可用代理（全部被拉黑或池为空）")
            return "", ""
        # Prefer US proxies, fall back to others
        us_candidates = [p for p in candidates
                         if (p.get("country_code") or "").upper() == "US"]
        chosen = random.choice(us_candidates if us_candidates else candidates)
        ip = chosen["ip"]
        port = chosen["port"]
        return f"socks5://{ip}:{port}", ip
    except Exception as e:
        logger.error(f"从代理池获取代理失败: {e}")
        return "", ""


def run_task(account_id, db, ocr_instance, lang_text, data_dir="data"):
    """Execute a check/unlock task for a single account. Returns True on success."""
    account = db.get_account(account_id)
    if not account:
        logger.error(f"Account {account_id} not found")
        return False
    if not account.get("enabled"):
        logger.info(f"Account {account_id} is disabled, skipping")
        return False

    settings = db.get_all_settings()
    pool_url = settings.get("proxy_pool_url", "").strip()

    max_proxy_retries = 3
    for proxy_attempt in range(max_proxy_retries + 1):
        result = _run_task_once(account_id, account, db, ocr_instance,
                                lang_text, data_dir, settings, pool_url)
        if result == "proxy_dead":
            if proxy_attempt < max_proxy_retries:
                logger.info(f"代理不可用，正在换代理重试 ({proxy_attempt + 1}/{max_proxy_retries})")
                continue
            else:
                logger.error(f"已重试 {max_proxy_retries} 次，所有代理均不可用")
                return False
        if result != "ip_blocked":
            return result == "success"
        if proxy_attempt < max_proxy_retries:
            logger.info(f"IP 被封禁，正在换代理重试 ({proxy_attempt + 1}/{max_proxy_retries})")
        else:
            logger.error(f"已重试 {max_proxy_retries} 次，所有代理均被封禁")
    return False


def _run_task_once(account_id, account, db, ocr_instance, lang_text,
                   data_dir, settings, pool_url):
    """Run task once. Returns 'success', 'ip_blocked', 'proxy_dead', or 'failed'."""

    # Get proxy: static proxy first, then pool
    proxy_row = None
    if account.get("proxy_id") and account["proxy_id"] > 0:
        proxy_row = db.get_proxy(account["proxy_id"])

    config = TaskConfig(account, settings, proxy_row)

    # If no static proxy and pool is configured, fetch from pool
    if not config.proxy and pool_url:
        pool_proxy, pool_ip = fetch_pool_proxy(pool_url, db)
        if pool_proxy:
            config.proxy = pool_proxy
            config.proxy_from_pool = True
            config.pool_ip = pool_ip
            logger.info(f"从代理池获取代理: {pool_proxy}")

    callbacks = TaskCallbacks(db, account_id, config, lang_text, data_dir)

    logger.info(f"{lang_text.CurrentAccount}{config.username}")

    driver = setup_driver(config)
    job_success = True
    ip_address = ""

    if not driver:
        # Pool proxy may cause WebDriver startup failure — retry with another
        if getattr(config, 'proxy_from_pool', False):
            logger.warning(f"代理 {config.proxy} 导致 WebDriver 启动失败，跳过")
            return "proxy_dead"
        callbacks.update_message(config.username, lang_text.failOnCallingWD)
        callbacks.notify(lang_text.failOnCallingWD)
        db.update_after_check(account_id, lang_text.failOnCallingWD)
        db.add_record(account_id, 0, lang_text.failOnCallingWD)
        return "failed"

    try:
        ip_address = get_ip(driver)

        # Pool proxy connectivity check: can't get IP means proxy is dead
        if not ip_address and getattr(config, 'proxy_from_pool', False):
            logger.warning(f"代理 {config.proxy} 无法连接，跳过")
            driver.quit()
            return "proxy_dead"

        aid = AppleIDAutomation(config, driver, ocr_instance, lang_text, callbacks)

        if aid.login():
            origin_password = aid.password
            # Check account status
            if aid.check_2fa():
                logger.info(lang_text.twoStepDetected)
                login_result = aid.unlock_2fa()
            elif not aid.check():
                logger.info(lang_text.accountLocked)
                login_result = aid.unlock()
            else:
                login_result = True
            logger.info(lang_text.checkComplete)

            # Update account info
            password_changed = origin_password != aid.password
            if password_changed:
                db.update_after_check(account_id, lang_text.normal, aid.password)
                callbacks.notify(f"{lang_text.updateSuccess}\n{lang_text.newPassword}{aid.password}")
            elif login_result:
                db.update_after_check(account_id, lang_text.normal)

            reset_result = True
            if login_result:
                # Auto reset password
                if config.enable_auto_update_password and not password_changed:
                    logger.info(lang_text.startChangePassword)
                    reset_pw_result = aid.change_password()
                    if reset_pw_result:
                        db.update_after_check(account_id, lang_text.normal, aid.password)
                        callbacks.notify(f"{lang_text.updateSuccess}\n{lang_text.newPassword}{aid.password}")
                    else:
                        logger.error(lang_text.FailToChangePassword)
                        callbacks.notify(lang_text.FailToChangePassword)
                        reset_result = False

                # Auto delete devices
                if reset_result and (config.enable_delete_devices or config.enable_check_password_correct):
                    need_login = False
                    apple_login_result = aid.login_appleid()
                    if config.enable_auto_update_password and not apple_login_result:
                        logger.error(lang_text.loginFail)
                        callbacks.record_error(driver)
                    else:
                        if not apple_login_result and config.enable_check_password_correct:
                            logger.info(lang_text.passwordChanged)
                            reset_pw_result = aid.change_password()
                            if reset_pw_result:
                                need_login = True
                                db.update_after_check(account_id, lang_text.normal, aid.password)
                                callbacks.notify(f"{lang_text.updateSuccess}\n{lang_text.newPassword}{aid.password}")
                            else:
                                logger.error(lang_text.FailToChangePassword)
                                callbacks.notify(lang_text.FailToChangePassword)
                        if config.enable_delete_devices:
                            if need_login:
                                apple_login_result = aid.login_appleid()
                            if apple_login_result:
                                aid.delete_devices()
                            else:
                                logger.error(lang_text.LoginFail)
                                callbacks.record_error(driver)
            else:
                logger.error(lang_text.UnlockFail)
                callbacks.notify(lang_text.UnlockFail)
                job_success = False
        else:
            logger.error(lang_text.missionFailed)
            job_success = False
    except BaseException:
        logger.error(lang_text.unknownError)
        traceback.print_exc()
        callbacks.record_error(driver)
        callbacks.update_message(config.username, lang_text.unknownError)
        callbacks.notify(lang_text.unknownError)
        job_success = False
    finally:
        try:
            driver.quit()
        except BaseException:
            logger.error(lang_text.WDCloseError)

    # Check if proxy was blocked (pool proxy) — signal retry
    if getattr(config, 'proxy_from_pool', False) and callbacks.proxy_was_blocked:
        pool_ip = getattr(config, 'pool_ip', '')
        if pool_ip:
            db.add_blacklist(pool_ip, "Apple IP blocked")
            logger.info(f"已将 {pool_ip} 加入黑名单")
        return "ip_blocked"

    # Record result
    if job_success:
        db.add_record(account_id, 1, lang_text.normal, ip_address)
        if not (config.enable_auto_update_password or
                config.enable_delete_devices or
                config.enable_check_password_correct):
            db.update_after_check(account_id, lang_text.normal)
    else:
        db.add_record(account_id, 0, lang_text.missionFailed, ip_address)
        db.update_after_check(account_id, lang_text.missionFailed)

    return "success" if job_success else "failed"
