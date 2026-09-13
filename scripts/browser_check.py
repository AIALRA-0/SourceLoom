"""Bounded browser walkthrough using isolated synthetic data and real HTTP."""

import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright


def main():
    out=Path('.local/browser');out.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();errors=[];checks=[];expected_failure=[False]
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={'width':1440,'height':1000},color_scheme='light')
        page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda e:errors.append(e.text) if e.type=='error' and not (expected_failure[0] and '503 (Service Unavailable)' in e.text) else None)
        page.goto('http://127.0.0.1:18765',wait_until='networkidle',timeout=15000)
        page.get_by_role('button',name='试用免费合成演示 ↗').click()
        page.get_by_role('heading',name='局部加速的边界 · 合成演示').wait_for()
        page.get_by_role('button',name='返回材料列表').click()
        page.get_by_role('heading',name='把原始资料，织成有据可循的教材').wait_for()
        page.screenshot(path=str(out/'home-desktop.png'),full_page=True)
        page.locator('[data-project]').first.click()
        for i in range(8):
            page.locator(f'nav [data-tab="{i}"]').click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'overflow at tab {i}'
            checks.append(f'desktop-tab-{i}')
        page.locator('nav [data-tab="4"]').click()
        page.get_by_role('button',name='移除一个条件段落').click()
        page.get_by_text('冻结义务在候选中没有落点',exact=True).wait_for()
        checks.append('omission-detected')
        page.screenshot(path=str(out/'omission-desktop.png'),full_page=True)
        page.get_by_role('button',name='只恢复被移除的段落').click()
        page.get_by_text('当前结构检查未发现缺口，语义是否保留仍需独立审核',exact=True).wait_for()
        checks.append('local-restore')
        page.locator('nav [data-tab="7"]').click()
        with page.expect_download() as info:
            page.get_by_role('link',name='下载原生候选包 ↓').click()
        info.value.save_as(str(out/'ReadWeave-Candidate.zip'))
        checks.append('native-zip-download')
        page.screenshot(path=str(out/'export-desktop.png'),full_page=True)
        page.get_by_role('button',name='切换明暗主题').click()
        page.screenshot(path=str(out/'export-dark.png'),full_page=True)
        for width in [768,390]:
            page.set_viewport_size({'width':width,'height':844})
            for i in range(8):
                page.locator(f'nav [data-tab="{i}"]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'overflow {width}/{i}'
                checks.append(f'width-{width}-tab-{i}')
            page.screenshot(path=str(out/f'export-{width}.png'),full_page=True)
        page.locator('nav [data-tab="0"]').click()
        page.get_by_role('button',name='返回材料列表').click()
        page.get_by_role('heading',name='把原始资料，织成有据可循的教材').wait_for()
        page.emulate_media(color_scheme='light')
        page.evaluate("document.documentElement.dataset.theme='light'")
        page.screenshot(path=str(out/'home-mobile.png'),full_page=True)
        # Failed external operations leave an explicit error and preserve the UI.
        expected_failure[0]=True
        page.route('**/api/projects',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"合成服务故障，材料保留"}'))
        page.get_by_role('button',name='刷新当前材料').click()
        page.get_by_role('status').get_by_text('合成服务故障，材料保留').wait_for()
        checks.append('service-error-visible')
        browser.close()
    report={'checks':checks,'count':len(checks),'errors':errors,'elapsed_seconds':round(time.monotonic()-start,2)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if errors:raise SystemExit(1)


if __name__=='__main__':main()
