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
        if in_price_area and classes.intersection({"promotion-price", "old-price"}):
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



def main():
    if not PRODUCTS.strip():
        raise SystemExit("Set the MONITOR_PRODUCTS secret")
    products = [row.split("|", 2) for row in PRODUCTS.splitlines() if row.strip()]
    failed = False
    code = os.environ.get("SMTP_CODE", "")
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch()
        for asin, threshold, name in products:
            limit = Decimal(threshold)
            page = browser.new_page(locale="zh-CN")
            try:
                url = product_url(asin)
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.locator(".promotion-price-area .promotion-price").first.wait_for(timeout=25_000)
                price = parse_price(page.content())
                print("Product price checked", flush=True)
                if price < limit:
                    if not (SENDER and RECIPIENT and code):
                        raise ValueError("Email secrets are missing")
                    send_mail(code, f"亚马逊海外购降价提醒：{name} ¥{price}",
                              f"{name}\n商品标价 ¥{price}，低于 ¥{limit}。\n商品链接：{url}")
            except Exception as exc:
                print(f"Check or notification failed ({type(exc).__name__})", flush=True)
                failed = True
            finally:
                page.close()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

