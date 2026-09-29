"""Drive the React admin through its visible Ant Design controls."""

import re

from playwright.sync_api import expect


def choose(page, selector, label):
    control = page.locator(selector)
    combo = (
        control
        if control.get_attribute("role") == "combobox"
        else control.get_by_role("combobox")
    )
    if combo.get_attribute("aria-expanded") != "true":
        control.click()
    expect(combo).to_have_attribute("aria-expanded", "true")
    expect(combo).to_have_attribute("aria-controls", re.compile(".+"))
    list_id = combo.get_attribute("aria-controls")
    dropdown = page.locator(".ant-select-dropdown").filter(
        has=page.locator(f'[id="{list_id}"]')
    )
    expect(dropdown).to_be_visible()
    expect(dropdown).not_to_have_class(re.compile("ant-slide-up-(appear|enter)"))
    option = dropdown.locator(".ant-select-item-option").filter(
        has=page.get_by_text(label, exact=True)
    )
    if option.count() == 0:
        if combo.get_attribute("readonly") is None:
            combo.fill(label)
        else:
            combo.press("End")
    option.click()
    page.keyboard.press("Escape")
    expect(dropdown).not_to_be_visible()


def expect_choice(page, selector, label):
    expect(
        page.locator(".ant-select").filter(has=page.locator(selector))
    ).to_contain_text(label)


def navigate(page, view):
    desktop = page.locator(f'.admin-sidebar [data-view="{view}"]')
    if desktop.is_visible():
        desktop.click()
    else:
        page.get_by_role("button", name="Open navigation").click()
        page.locator(f'.admin-mobile-nav [data-view="{view}"]').click()


def close_annotator(page):
    drawer = page.locator(".admin-annotator-drawer")
    if drawer.is_visible():
        drawer.get_by_role("button", name="Close", exact=True).click()
        expect(drawer).not_to_be_visible()


def open_annotator(page, user_id):
    close_annotator(page)
    navigate(page, "annotators")
    page.get_by_test_id("annotator-table").locator(
        f'tr[data-row-key="{user_id}"]'
    ).get_by_role(
        "button",
        name="View",
        exact=True,
    ).click()
    expect(page.locator(".admin-annotator-drawer")).to_be_visible()
