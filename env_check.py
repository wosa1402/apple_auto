import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _sudo_prefix():
    """Return ['sudo'] if sudo is available and we're not root, else []."""
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    return []


def _run(cmd, timeout=120, shell=False):
    """Run a command with optional sudo prefix."""
    if isinstance(cmd, list):
        cmd = _sudo_prefix() + cmd
    elif shell and _sudo_prefix():
        cmd = "sudo " + cmd
    return subprocess.run(
        cmd, check=True, capture_output=True, timeout=timeout, shell=shell,
    )


def find_chrome():
    """Try to find Chrome/Chromium binary on the system."""
    names = [
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
    ]
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    # Check common install paths
    common_paths = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/opt/google/chrome/google-chrome",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return None


def find_chromedriver():
    """Try to find chromedriver binary on the system."""
    path = shutil.which("chromedriver")
    if path:
        return path
    common_paths = [
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return None


def try_install_chrome():
    """Attempt to auto-install Chrome on Linux. Returns True on success."""
    import platform
    if platform.system() != "Linux":
        logger.warning("自动安装 Chrome 仅支持 Linux 系统")
        return False

    logger.info("正在尝试自动安装 Google Chrome...")

    # Try apt (Debian/Ubuntu)
    if shutil.which("apt-get"):
        try:
            _run(["apt-get", "update", "-qq"], timeout=120)

            # Try Google Chrome via direct download (most compatible)
            try:
                _run(["apt-get", "install", "-y", "-qq", "wget", "gnupg"], timeout=120)
                _run(
                    "wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
                    shell=True, timeout=120,
                )
                _run(["apt-get", "install", "-y", "-qq", "/tmp/chrome.deb"], timeout=300)
                subprocess.run(["rm", "-f", "/tmp/chrome.deb"], capture_output=True)
                logger.info("Google Chrome 安装成功")
                return True
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.info(f"直接下载 Chrome 失败，尝试其他方式: {e}")
                # Try to fix broken dependencies
                try:
                    _run(["apt-get", "install", "-f", "-y", "-qq"], timeout=120)
                    if find_chrome():
                        logger.info("Google Chrome 安装成功（已修复依赖）")
                        return True
                except Exception:
                    pass

            # Try installing chromium as fallback
            try:
                _run(["apt-get", "install", "-y", "-qq", "chromium-browser"], timeout=300)
                logger.info("Chromium 安装成功")
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

            # Try chromium package name (varies by distro)
            try:
                _run(["apt-get", "install", "-y", "-qq", "chromium"], timeout=300)
                logger.info("Chromium 安装成功")
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"apt-get 更新失败: {e}")

    # Try yum/dnf (RHEL/CentOS/Fedora)
    pkg_mgr = shutil.which("dnf") or shutil.which("yum")
    if pkg_mgr:
        try:
            _run([pkg_mgr, "install", "-y", "chromium"], timeout=300)
            logger.info("Chromium 安装成功")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"通过 {pkg_mgr} 安装 Chromium 失败: {e}")

    logger.warning("无法自动安装 Chrome，请手动安装或配置远程 WebDriver")
    return False


def try_install_chromedriver():
    """Attempt to install chromedriver. Returns True on success."""
    # Selenium 4.6+ has SeleniumManager which auto-downloads chromedriver
    # So we just need to verify selenium is available
    try:
        import selenium
        version = tuple(int(x) for x in selenium.__version__.split(".")[:2])
        if version >= (4, 6):
            logger.info("Selenium >= 4.6 检测到，将自动管理 ChromeDriver")
            return True
    except Exception:
        pass

    # Fallback: try to install via apt
    if shutil.which("apt-get"):
        try:
            _run(["apt-get", "install", "-y", "-qq", "chromium-chromedriver"], timeout=300)
            logger.info("ChromeDriver 安装成功")
            return True
        except Exception:
            pass

    return False


def check_environment():
    """Check WebDriver environment and attempt auto-setup.

    Returns a dict with status information:
        {
            "chrome_path": str or None,
            "chromedriver_path": str or None,
            "chrome_ok": bool,
            "driver_ok": bool,
            "ready": bool,          # True if local WebDriver is usable
            "message": str,         # Status message for display
            "auto_installed": bool, # True if auto-install was performed
        }
    """
    result = {
        "chrome_path": None,
        "chromedriver_path": None,
        "chrome_ok": False,
        "driver_ok": False,
        "ready": False,
        "message": "",
        "auto_installed": False,
    }

    # Check Chrome
    chrome_path = find_chrome()
    if chrome_path:
        result["chrome_path"] = chrome_path
        result["chrome_ok"] = True
        logger.info(f"检测到 Chrome: {chrome_path}")
    else:
        logger.warning("未检测到 Chrome 浏览器，尝试自动安装...")
        if try_install_chrome():
            chrome_path = find_chrome()
            if chrome_path:
                result["chrome_path"] = chrome_path
                result["chrome_ok"] = True
                result["auto_installed"] = True
                logger.info(f"Chrome 已自动安装: {chrome_path}")

    # Check ChromeDriver
    chromedriver_path = find_chromedriver()
    if chromedriver_path:
        result["chromedriver_path"] = chromedriver_path
        result["driver_ok"] = True
        logger.info(f"检测到 ChromeDriver: {chromedriver_path}")
    else:
        if try_install_chromedriver():
            chromedriver_path = find_chromedriver()
            if chromedriver_path:
                result["chromedriver_path"] = chromedriver_path
                result["auto_installed"] = True
            result["driver_ok"] = True  # Selenium Manager will handle it
            logger.info("ChromeDriver 就绪（Selenium 自动管理）")

    # Determine overall status
    if result["chrome_ok"] and result["driver_ok"]:
        result["ready"] = True
        result["message"] = "本地 WebDriver 环境就绪"
    elif result["chrome_ok"]:
        result["ready"] = False
        result["message"] = "Chrome 已安装，但 ChromeDriver 未就绪"
    else:
        result["ready"] = False
        result["message"] = (
            "本地未检测到 Chrome 浏览器且自动安装失败。\n"
            "请选择以下方式之一：\n"
            "1. 手动安装 Chrome 浏览器\n"
            "2. 在「系统设置」中配置远程 WebDriver URL"
        )

    return result
