import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from html.parser import HTMLParser
import os
import re
import smtplib
import ssl

from playwright.sync_api import sync_playwright

PRODUCTS = os.environ.get("MONITOR_PRODUCTS", "")

SENDER = os.environ.get("SMTP_USER", "")
RECIPIENT = os.environ.get("MAIL_TO") or SENDER

class PriceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parent = []
        self.prices = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"}:
            return
        classes = set(dict(attrs).get("class", "").split())
        in_price_area = any("promotion-price-area" in node[1] for node in self.parent)
        if (in_price_area and classes.intersection({"promotion-price", "old-price"})) or "price-deal__original-price" in classes:
            self.current = []
            self.prices.append(self.current)
        self.parent.append((tag, classes, self.current))

    def handle_endtag(self, tag):
        for index in range(len(self.parent) - 1, -1, -1):
            if self.parent[index][0] == tag:
                del self.parent[index:]
                self.current = self.parent[-1][2] if self.parent else None
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data.strip())


def parse_price(html):
    parser = PriceParser()
    parser.feed(html)
    prices = []
    for parts in parser.prices:
        match = re.fullmatch(r"[¥￥]([0-9,]+(?:\.[0-9]{1,2})?)",
                             "".join(parts))
        if match:
            prices.append(Decimal(match[1].replace(",", "")))
    if len(prices) != 1 or prices[0] <= 0:
        raise ValueError("未能唯一识别商品主价格区中的人民币价格")
    return prices[0]


def product_url(asin):
    return f"https://globalstore.amazon.cn/detailPage/{asin}"


def send_mail(code, subject, body):
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = RECIPIENT
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=25,
                          context=ssl.create_default_context()) as smtp:
        smtp.login(SENDER, code)
        smtp.send_message(message)



def write_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    checked_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as summary:
        summary.write(f"## 查价状态\n\n最近查价时间：{checked_at}（北京时间）\n\n")
        summary.write("| 商品 | 最新标价 | 提醒价（低于时发邮件） | 读取状态 | 邮件状态 |\n| --- | --- | --- | --- | --- |\n")
        for name, price, read_status, mail_status, *threshold in rows:
            name = str(name).replace("|", "&#124;").replace("\n", " ")
            listed_price = f"¥{price:.2f}" if price is not None else "—"
            alert_price = f"¥{Decimal(threshold[0]):.2f}" if threshold else "—"
            summary.write(f"| {name} | {listed_price} | {alert_price} | {read_status} | {mail_status} |\n")


def load_products():
    path = Path("products.enc")
    if path.exists():
        from cryptography.fernet import Fernet
        return json.loads(Fernet(os.environ["PRODUCT_CONFIG_KEY"]).decrypt(path.read_bytes()))
    return [row.split("|", 2) for row in PRODUCTS.splitlines() if row.strip()]


def manage_products(action):
    from cryptography.fernet import Fernet

    key = Fernet(os.environ["PRODUCT_CONFIG_KEY"])
    inputs = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")).get("inputs", {})
    product = inputs.get("product", "").strip()
    match = re.fullmatch(r"(?:https://globalstore\.amazon\.cn/detailPage/)?([A-Za-z0-9]{10})(?:[/?#].*)?", product)
    if not match:
        raise SystemExit("请输入有效的海外购商品链接或 10 位 ASIN。")
    asin = match[1].upper()
    products = load_products()
    existing = any(item[0] == asin for item in products)
    if action == "delete":
        if not existing:
            raise SystemExit("此商品不在监控列表中。")
        products = [item for item in products if item[0] != asin]
    else:
        if existing:
            raise SystemExit("此商品已在监控列表中。")
        limit = inputs.get("price_limit", "").strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", limit) or Decimal(limit) <= 0:
            raise SystemExit("请输入大于零、最多两位小数的提醒价。")
        with sync_playwright() as api:
            browser = api.chromium.launch()
            page = browser.new_page(locale="zh-CN")
            try:
                page.goto(product_url(asin), wait_until="domcontentloaded", timeout=45_000)
                title = page.locator(".title-area").first
                title.wait_for(timeout=25_000)
                name = " ".join(title.inner_text().split())
                if not name:
                    raise SystemExit("商品页没有返回名称。")
            finally:
                browser.close()
        products.append([asin, limit, name])
    Path("products.enc").write_bytes(key.encrypt(json.dumps(products, ensure_ascii=False).encode()))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"## 商品列表已更新\n\n当前监控 {len(products)} 件商品。\n")
    return 0


def main():
    action = os.environ.get("MONITOR_ACTION") or "check"
    if action in {"add", "delete"}:
        return manage_products(action)
    products = load_products()
    if not products:
        write_summary([])
        return 0
    failed = False
    rows = []
    code = os.environ.get("SMTP_CODE", "")
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch()
        for asin, threshold, name in products:
            price = None
            read_status = "失败"
            mail_status = "未检查"
            page = None
            try:
                limit = Decimal(threshold)
                page = browser.new_page(locale="zh-CN")
                url = product_url(asin)
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.locator(".promotion-price-area .promotion-price, .price-deal__original-price").first.wait_for(timeout=25_000)
                price = parse_price(page.content())
                read_status = "成功"
                mail_status = "未触发"
                print("Product price checked", flush=True)
                if price < limit:
                    mail_status = "发送失败"
                    if not (SENDER and RECIPIENT and code):
                        raise ValueError("Email secrets are missing")
                    send_mail(code, f"亚马逊海外购降价提醒：{name} ¥{price}",
                              f"{name}\n商品标价 ¥{price}，低于 ¥{limit}。\n商品链接：{url}")
                    mail_status = "已发送"
            except Exception as exc:
                print(f"Check or notification failed ({type(exc).__name__})", flush=True)
                failed = True
            finally:
                rows.append((f"{name} · {asin}", price, read_status, mail_status, threshold))
                if page is not None:
                    page.close()
    write_summary(rows)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())




