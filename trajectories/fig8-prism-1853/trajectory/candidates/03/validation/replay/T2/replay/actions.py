
import json, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
cfg = json.loads(Path(sys.argv[1]).read_text())
out = Path(cfg['output'])
observed = {'completed_actions': [], 'state': {}, 'dom': '', 'url': ''}
def save():
    observed['state'] = page.evaluate('(tag)=>{const n=document.getElementById(tag);return n?JSON.parse(n.textContent):{}}', cfg['tag'])
    observed['dom'] = page.locator('body').inner_text()[:12000]
    observed['html'] = page.locator('body').inner_html()[:24000]
    observed['url'] = page.url
    observed['actual_scene'] = page.locator('body :not(script)').count() > 0 or bool(observed['dom'])
    screenshot = out / ('step_%02d.png' % len(observed['completed_actions']))
    page.screenshot(path=str(screenshot))
    observed['screenshot'] = str(screenshot)
    (out / 'observation.json').write_text(json.dumps(observed, ensure_ascii=False))
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(cfg['endpoint'])
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({'width': 1280, 'height': 720})
    page.set_default_timeout(5000)
    for action in cfg['actions']:
        kind, target, value = action['kind'], action['target'], action['value']
        if kind == 'open':
            path = (Path(cfg['repo']) / target).resolve() if target else Path(cfg['target'])
            assert path.is_relative_to(Path(cfg['repo'])) and path.is_file(), 'local_page_required'
            page.goto(path.as_uri(), wait_until='load', timeout=15000)
            page.wait_for_timeout(350)
        elif kind == 'click':
            page.locator(target).click()
            page.wait_for_timeout(100)
        elif kind == 'fill':
            page.locator(target).fill(value)
            page.wait_for_timeout(100)
        elif kind == 'scroll':
            page.mouse.wheel(0, int(value))
            page.wait_for_timeout(150)
        elif kind == 'dom':
            observed['query'] = page.locator(target or 'body').evaluate_all(
                '(nodes)=>nodes.slice(0,30).map(n=>({tag:n.tagName,text:n.innerText,value:n.value,html:n.outerHTML.slice(0,4000)}))')
        elif kind == 'screenshot':
            page.screenshot(path=str(out / 'screenshot.png'))
        observed['completed_actions'].append(action)
        save()
    observed['state'] = page.evaluate('(tag)=>{const n=document.getElementById(tag);return n?JSON.parse(n.textContent):{}}', cfg['tag'])
    observed['dom'] = page.locator('body').inner_text()[:12000]
    observed['html'] = page.locator('body').inner_html()[:24000]
    observed['url'] = page.url
    observed['actual_scene'] = page.locator('body :not(script)').count() > 0 or bool(observed['dom'])
    page.screenshot(path=str(out / 'screenshot.png'))
    observed['screenshot'] = str(out / 'screenshot.png')
    save()
