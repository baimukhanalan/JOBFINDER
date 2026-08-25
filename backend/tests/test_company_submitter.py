import asyncio

from backend.tools.company_submitter import submit_and_confirm


class EmptyLocator:
    async def count(self):
        return 0


class BodyLocator:
    def __init__(self, page):
        self.page = page

    async def inner_text(self, timeout=None):
        return self.page.body


class Control:
    def __init__(self, page):
        self.page = page

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def evaluate(self, script):
        return "BUTTON|submit|send||Submit application"

    async def inner_text(self):
        return "Submit application"

    async def click(self, timeout=None):
        self.page.clicks += 1
        self.page.body = self.page.after_body
        self.page.url = self.page.after_url


class OneLocator:
    def __init__(self, control):
        self.control = control

    async def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return self.control


class Page:
    def __init__(self, *, after_body="Thank you for your application",
                 after_url="https://jobs.test/thank-you", captcha=False):
        self.url = "https://jobs.test/apply"
        self.body = "CAPTCHA" if captcha else "Application form"
        self.after_body = after_body
        self.after_url = after_url
        self.clicks = 0
        self.control = Control(self)

    def locator(self, selector):
        if selector == "body":
            return BodyLocator(self)
        if selector == 'button[type="submit"]':
            return OneLocator(self.control)
        return EmptyLocator()

    async def wait_for_timeout(self, milliseconds):
        return None

    async def screenshot(self, **kwargs):
        return None


def test_positive_confirmation_is_required_after_one_click(tmp_path):
    page = Page()
    result = asyncio.run(submit_and_confirm(page, artifact_dir=tmp_path, settle_ms=1))
    assert result["confirmed"] is True
    assert result["signal"] == "confirmation_text"
    assert page.clicks == 1


def test_ambiguous_post_click_result_is_not_confirmed():
    page = Page(after_body="Application form", after_url="https://jobs.test/apply")
    result = asyncio.run(submit_and_confirm(page, settle_ms=1))
    assert result == {
        "confirmed": False,
        "clicked": True,
        "control_label": "Submit application",
        "before_url": "https://jobs.test/apply",
        "after_url": "https://jobs.test/apply",
        "signal": "none",
        "reason": "ambiguous_after_click",
    }
    assert page.clicks == 1


def test_captcha_blocks_click_fail_closed():
    page = Page(captcha=True)
    result = asyncio.run(submit_and_confirm(page, settle_ms=1))
    assert result["reason"] == "captcha_or_challenge"
    assert result["clicked"] is False
    assert page.clicks == 0

