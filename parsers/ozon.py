import json
import logging
import random
import re
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ====================== ГЛОБАЛЬНЫЕ НАСТРОЙКИ ======================
TARGET_PRODUCT_COUNT = 20                    # Сколько товаров нужно спарсить

# === Настройки борьбы с AntiBot ===
MAX_ANTIBOT_ATTEMPTS = 3                    # Максимальное количество попыток на один товар
ANTIBOT_WAIT_BEFORE_RETRY = 5.0             # секунд ожидания перед повторной попыткой (без refresh)
ANTIBOT_WAIT_AFTER_REFRESH = 4.0            # секунд ожидания после refresh страницы


# Пути
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
LOG_FILE = LOG_DIR / "ozon_parser.log"

# Маркеры AntiBot-страницы
ANTIBOT_MARKERS = [
    "сопоставьте пазл", "двигая ползунок", "robot", "captcha",
    "verify you are human", "подтвердите, что вы не робот",
]

# Маркеры окончания разделов
SECTION_END_MARKERS = [
    "Характеристики", "Описание", "Отзывы о товаре", "Подобрали для вас",
    "Покупают вместе", "Рекомендуем также", "Похожие", "Наведите камеру",
    "О магазине", "Доставка и возврат",
]

# Строки, которые нужно игнорировать при парсинге
NOISE_LINES = {
    "Характеристики", "Описание", "Комплектация", "Подобрали для вас",
    "Покупают вместе", "Рекомендуем также", "Похожие", "Отзывы о товаре",
    "Вопросы о товаре", "Наведите камеру и скачайте бесплатное приложение Ozon",
    "О магазине", "Доставка и возврат",
    "Информация о технических характеристиках, комплекте поставки, стране изготовления, внешнем виде и цвете товара носит справочный характер и основывается на последних доступных к моменту публикации сведениях",
}

def setup_logger() -> logging.Logger:
    """Настраивает и возвращает логгер для Ozon."""
    logger = logging.getLogger("parsers.ozon")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info("=== ЛОГГЕР OZON ИНИЦИАЛИЗИРОВАН ===")
    logger.info("Файл лога: %s", LOG_FILE)
    return logger


logger = setup_logger()


def get_driver():
    """Создаёт и возвращает undetected Chrome driver."""
    logger.info("Создаём undetected_chromedriver...")

    options = uc.ChromeOptions()
    options.page_load_strategy = "eager"
    # options.add_argument("--headless=new")          # раскомментировать при необходимости

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )

    driver = uc.Chrome(options=options, use_subprocess=True)
    driver.set_page_load_timeout(15)

    logger.info("Браузер успешно создан")
    return driver


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def normalize_url(url: str) -> str:
    """Приводит относительные ссылки к полному виду."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.ozon.ru" + url
    return url


def is_product_url(url: str) -> bool:
    """Проверяет, является ли ссылка страницей товара."""
    return bool(url and ("/product/" in url or "/context/detail/id/" in url))


def clean_text(value: str) -> str:
    """Очищает текст от множественных пробелов."""
    return re.sub(r"\s+", " ", value or "").strip()


def get_body_text(driver) -> str:
    """Возвращает весь видимый текст страницы (fallback)."""
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def is_antibot_page(driver, extra_text: str = "") -> bool:
    """Проверяет наличие AntiBot-защиты на странице."""
    haystack = " ".join([
        clean_text(driver.title),
        clean_text(get_body_text(driver)),
        clean_text(extra_text)
    ]).lower()
    result = any(marker in haystack for marker in ANTIBOT_MARKERS)
    logger.debug("Проверка на AntiBot: %s", "ОБНАРУЖЕН" if result else "нет")
    return result


def collect_product_links(driver, max_items: int) -> list[str]:
    """Собирает ссылки на товары со страницы поиска."""
    logger.info("Начинаем сбор ссылок на товары (нужно собрать %d)", max_items)
    links: list[str] = []
    seen = set()
    scroll_rounds = 8

    for step in range(scroll_rounds):
        new_links = 0
        for a in driver.find_elements(By.XPATH, "//a[@href]"):
            try:
                href = normalize_url(a.get_attribute("href") or "")
                if is_product_url(href) and href not in seen:
                    seen.add(href)
                    links.append(href)
                    new_links += 1
            except Exception:
                continue

        logger.debug("Скролл %d/%d: найдено %d новых ссылок | Всего: %d",
                     step + 1, scroll_rounds, new_links, len(links))

        if len(links) >= max_items:
            logger.info("Достигнут лимит %d товаров, прерываем сбор", max_items)
            break

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(0.6, 1.1))

    logger.info("Сбор ссылок завершён. Собрано: %d товаров", len(links))
    return links


def extract_section_text(full_text: str, start_marker: str, end_markers: list[str]) -> str:
    """Вырезает текст между начальным маркером и первым из конечных."""
    if not full_text:
        return ""
    start = full_text.find(start_marker)
    if start == -1:
        return ""
    after = full_text[start + len(start_marker):]
    end = len(after)
    for marker in end_markers:
        idx = after.find(marker)
        if idx != -1 and idx < end:
            end = idx
    return after[:end].strip()


def looks_like_key(line: str) -> bool:
    """Определяет, похожа ли строка на ключ характеристики."""
    line = clean_text(line)
    if (not line or line in NOISE_LINES or len(line) > 90 or
        len(line.split()) > 10 or not re.search(r"[A-Za-zА-Яа-яЁё]", line) or
        re.fullmatch(r"[\d\s.,₽%\-+]+", line)):
        return False
    return True


def parse_key_value_lines(lines: list[str]) -> dict[str, str]:
    """Парсит ключ-значение из списка строк (fallback)."""
    result: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = clean_text(lines[i])
        if not line or line in NOISE_LINES:
            i += 1
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            key = clean_text(key)
            value = clean_text(value)
            if key and value and looks_like_key(key):
                result[key] = value
                i += 1
                continue

        if looks_like_key(line):
            key = line
            j = i + 1
            values = []
            while j < len(lines):
                next_line = clean_text(lines[j])
                if not next_line or next_line in NOISE_LINES:
                    j += 1
                    continue
                if looks_like_key(next_line):
                    break
                values.append(next_line)
                j += 1
            value = clean_text(" ".join(values))
            if key and value:
                result[key] = value
                i = j
                continue
        i += 1
    return result


def extract_price(driver) -> str:
    """Извлекает цену товара (meta-теги → XPath)."""
    logger.debug("Начинаем извлечение цены...")

    # Meta-теги
    meta_selectors = [
        'meta[itemprop="price"]',
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]'
    ]
    for sel in meta_selectors:
        try:
            for e in driver.find_elements(By.CSS_SELECTOR, sel):
                content = clean_text(e.get_attribute("content") or "")
                if content and re.search(r"\d", content):
                    price = f"{content} ₽"
                    logger.debug("Цена найдена через meta-тег %s: %s", sel, price)
                    return price
        except Exception:
            pass

    # Fallback через видимый текст
    xpaths = [
        "//*[contains(@data-testid, 'price')]",
        "//span[contains(@class, 'price')]",
        "//div[contains(@class, 'price')]",
        "//*[contains(text(), '₽')]"
    ]
    for xp in xpaths:
        try:
            for e in driver.find_elements(By.XPATH, xp):
                txt = clean_text(e.text)
                if ("₽" in txt and re.search(r"\d", txt) and
                        txt.lower() not in {"товары за 1₽", "цена что надо"}):
                    if len(txt) < 60 or re.search(r"\d[\d\s]*₽", txt):
                        logger.debug("Цена найдена через XPath %s: %s", xp, txt)
                        return txt
        except Exception:
            continue

    logger.warning("Цена НЕ НАЙДЕНА")
    return "Не найдено"


def extract_description(driver) -> str:
    """Извлекает описание товара."""
    logger.debug("Начинаем извлечение описания...")
    body_text = get_body_text(driver)

    desc = extract_section_text(body_text, "Описание", SECTION_END_MARKERS)
    desc = clean_text(desc)
    desc = re.sub(r"Показать полностью.*?(?=Комплектация|Характеристики|Отзывы|$)", "", desc, flags=re.S | re.I)
    desc = re.sub(r"#\S+", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    if len(desc) > 80:
        return desc

    # Fallback-селекторы
    selectors = [
        "//div[@data-testid='pdp-description']",
        "//div[contains(@class, 'pdp-description')]",
        "//section[contains(., 'Описание')]//div[last()]",
        "//h2[contains(., 'Описание')]/following-sibling::div[1]"
    ]
    for sel in selectors:
        try:
            txt = clean_text(driver.find_element(By.XPATH, sel).text)
            if len(txt) > 100:
                return txt
        except Exception:
            continue

    return "Не найдено"


def extract_characteristics(driver) -> dict[str, str]:
    """Извлекает все характеристики товара (основной + резервные методы)."""
    logger.debug("Начинаем извлечение характеристик...")
    characteristics: dict[str, str] = {}

    # 1. Основной парсер
    try:
        dl_items = driver.find_elements(By.XPATH, "//dl[contains(@class, 'pdp_a9i')]")
        for dl in dl_items:
            try:
                dt = dl.find_element(By.XPATH, ".//dt")
                key_text = clean_text(dt.text).replace(":", "").strip()
                key = key_text

                # Пропускаем широкий блок "Предназначено для", чтобы избежать дублирования
                if "pdp_ai9" in dl.get_attribute("class") and "Предназначено для" in key:
                    continue

                dd = dl.find_element(By.XPATH, ".//dd[contains(@class, 'pdp_ia8')]")
                value = clean_text(dd.text)

                if looks_like_key(key) and value and "Информация о технических характеристиках" not in value:
                    characteristics[key] = value
            except Exception:
                continue
        logger.debug("Найдено через pdp_a9i: %d характеристик", len(characteristics))
    except Exception as e:
        logger.debug("Новый pdp-парсер не сработал: %s", e)

    # 2. Старый парсер (резерв)
    try:
        items = driver.find_elements(By.XPATH,
            "//div[contains(@data-testid, 'specification') or contains(@class, 'specification__item') or contains(@class, 'char-item')] | "
            "//div[contains(@class, '_name') and following-sibling::div[contains(@class, '_value')]]"
        )
        for item in items:
            texts = [clean_text(t.text) for t in item.find_elements(By.XPATH, ".//div | .//span | .//p")]
            if len(texts) >= 2:
                key, value = texts[0].strip(), " ".join(texts[1:]).strip()
                if looks_like_key(key) and value and "Информация о технических характеристиках" not in value:
                    characteristics[key] = value
    except Exception:
        pass

    # 3. Fallback - парсинг текста всей страницы
    body_text = get_body_text(driver)
    section = extract_section_text(body_text, "Характеристики", ["Отзывы о товаре", "Подобрали для вас", "Покупают вместе", "Рекомендуем также"])
    if section:
        lines = [clean_text(line) for line in section.splitlines() if line and line not in NOISE_LINES]
        text_chars = parse_key_value_lines(lines)
        characteristics.update(text_chars)

    # Финальная очистка
    cleaned = {}
    disclaimer = "Информация о технических характеристиках, комплекте поставки"
    for k, v in characteristics.items():
        if len(k) < 2 or len(v) < 1 or k in NOISE_LINES or disclaimer in v:
            continue
        if v.strip().startswith("Предназначено для:") or "Предназначено для: " in v:
            continue
        if k.strip().startswith("Предназначено для:"):
            continue
        cleaned[k] = v

    logger.info("ИТОГО извлечено характеристик: %d", len(cleaned))
    return cleaned


def expand_all_hidden_content(driver):
    """Раскрывает все скрытые блоки («Показать полностью» и т.п.)."""
    logger.debug("=== РАСКРЫВАЕМ СКРЫТЫЙ КОНТЕНТ ===")
    xpaths = [
        "//button[contains(., 'Показать полностью')]",
        "//button[contains(., 'Все характеристики')]",
        "//button[contains(., 'Показать все')]",
        "//button[contains(., 'развернуть') or contains(., 'Развернуть')]",
        "//button[contains(@class, 'expand') or contains(@class, 'more')]",
        "//button[contains(@data-testid, 'expand')]",
        "//span[contains(., 'Показать полностью')]/parent::button",
    ]
    clicked = 0
    for xpath in xpaths:
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
            for btn in buttons:
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.3)
                    btn.click()
                    clicked += 1
                    time.sleep(random.uniform(0.8, 1.5))
                    logger.debug("Кликнули кнопку: %s", xpath)
        except Exception:
            pass

    if clicked:
        logger.info("Раскрыто скрытых блоков: %d кнопок", clicked)
        time.sleep(0.7)
    else:
        logger.debug("Кнопок «Показать полностью» не найдено")


def safe_text(driver, by, selector, default="Не найдено") -> str:
    """Безопасное получение текста элемента."""
    try:
        elems = driver.find_elements(by, selector)
        if elems:
            return clean_text(elems[0].text) or default
    except Exception:
        pass
    return default


# ====================== ПАРСИНГ ОДНОЙ КАРТОЧКИ ======================
def parse_product_page(driver, url: str, product_index: int, total: int) -> dict[str, object] | None:
    """Парсит одну страницу товара."""
    logger.info("=== НАЧИНАЕМ ПАРСИНГ ТОВАРА %d/%d ===", product_index, total)
    logger.info("URL: %s", url)

    for attempt in range(1, MAX_ANTIBOT_ATTEMPTS + 1):
        logger.debug("Попытка %d/%d для товара %d", attempt, MAX_ANTIBOT_ATTEMPTS, product_index)

        if product_index <= 4:
            time.sleep(random.uniform(2.0, 3.0))
        else:
            time.sleep(random.uniform(1.0, 2.0))

        driver.get(url)
        time.sleep(random.uniform(1.0, 2.0))

        try:
            WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "h1")))
        except TimeoutException:
            pass

        expand_all_hidden_content(driver)          # важно для раскрытия характеристик

        title_text = safe_text(driver, By.TAG_NAME, "h1")

        if not is_antibot_page(driver, extra_text=title_text):
            logger.info("AntiBot НЕ обнаружен на попытке %d — успешно", attempt)
            break

        logger.warning("ANTI BOT ОБНАРУЖЕН на попытке %d/%d (товар %d)", attempt, MAX_ANTIBOT_ATTEMPTS, product_index)
        print(f"\n⚠️ ANTI BOT на товаре {product_index}/{total} (попытка {attempt}/{MAX_ANTIBOT_ATTEMPTS})")

        if attempt == 1:
            time.sleep(ANTIBOT_WAIT_BEFORE_RETRY)
            continue
        elif attempt == 2:
            driver.refresh()
            time.sleep(ANTIBOT_WAIT_AFTER_REFRESH)
            continue
        else:
            logger.warning("Все %d попытки не помогли — товар будет отложен", MAX_ANTIBOT_ATTEMPTS)
            break

    if is_antibot_page(driver):
        logger.warning("Товар %d/%d ОТЛОЖЕН — AntiBot не прошёл", product_index, total)
        print(f"Товар {product_index}/{total} → AntiBot, отложен на повторную проверку")
        return None

    name = title_text if title_text and title_text != "Не найдено" else clean_text(driver.title) or "Не найдено"
    price = extract_price(driver)
    description = extract_description(driver)
    characteristics = extract_characteristics(driver)

    logger.info("=== ТОВАР %d УСПЕШНО СПАРСЕН ===", product_index)

    return {
        "link": url,
        "name": name,
        "price": price,
        "description": description,
        "characteristics": characteristics,
    }


# ====================== MAIN ======================
def main(url: str = None, output: str = None):
    """Основная функция программы (можно вызывать из run.py)."""
    logger.info("=== ЗАПУСК ПАРСЕРА OZON ===")
    logger.info("Целевое количество товаров: %d", TARGET_PRODUCT_COUNT)

    if not url:
        url = input("🔗 Вставьте ссылку на страницу поиска Ozon: ").strip()

    # Создаём папки, если их нет
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    driver = None
    products: list[dict[str, object]] = []
    failed_links: list[str] = []

    try:
        driver = get_driver()
        print("Открываем страницу поиска...")
        driver.get(url)
        time.sleep(4)
        WebDriverWait(driver, 8).until(lambda d: len(d.find_elements(By.XPATH, "//a[@href]")) > 0)

        links = collect_product_links(driver, max_items=TARGET_PRODUCT_COUNT)
        print(f"Найдено товаров: {len(links)}\n")

        to_process = min(TARGET_PRODUCT_COUNT, len(links))

        for i, link in enumerate(links[:to_process], start=1):
            print(f"Товар {i}/{to_process}")
            try:
                product = parse_product_page(driver, link, i, to_process)
                if product is None:
                    failed_links.append(link)
                    continue
                products.append(product)
                print(f"Товар {i}/{to_process} → OK: {product['name'][:100]}...")
                time.sleep(random.uniform(3.0, 5.0))
            except Exception as e:
                logger.error("Ошибка на товаре %d: %s", i, e)
                print(f"Товар {i}/{to_process} → Ошибка")

        if failed_links:
            print(f"\n🔄 ПОВТОРНАЯ ПРОВЕРКА {len(failed_links)} товаров (AntiBot)...")
            for idx, link in enumerate(failed_links, start=1):
                print(f"Повтор товара {idx}/{len(failed_links)}")
                try:
                    product = parse_product_page(driver, link, idx, len(failed_links))
                    if product:
                        products.append(product)
                        print(f"Повтор {idx} → OK")
                    else:
                        print(f"🔴 НЕ УДАЛОСЬ СПАРСИТЬ (AntiBot):")
                        print(f"   → {link}")
                except Exception:
                    print(f"Повтор {idx} → Ошибка")

    except KeyboardInterrupt:
        print("\nПрервано пользователем")
    except Exception as e:
        logger.error("ФАТАЛЬНАЯ ОШИБКА: %s", e)
        print("Критическая ошибка!")
    finally:
        if 'driver' in locals() and driver is not None:
            try:
                driver.quit()
            except:
                pass

        # Сохраняем результат в папку output/
        output_filename = output or "ozon_products.json"
        output_path = OUTPUT_DIR / output_filename

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

        print(f"\nГотово ✅! Собрано товаров: {len(products)}")
        print(f"Файл сохранён: {output_path}\n")
        print("Если появилась ошибка ниже - игнорируйте её, она harmless:")
        logger.info("=== ПАРСЕР OZON ЗАВЕРШИЛ РАБОТУ УСПЕШНО ===")


if __name__ == "__main__":
    main()