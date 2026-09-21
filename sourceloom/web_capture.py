"""Rendered-web acquisition before semantic generation

The browser discovers objects after client-side code and lazy loading have run
The returned manifest is evidence for completeness, while later models decide
what each captured object means in the article
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from urllib.parse import urlsplit


def capture(url: str, timeout_ms: int = 30000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(
            executable_path='/usr/bin/google-chrome' if __import__('os').path.exists('/usr/bin/google-chrome') else None,
            headless=True,
            # Production already runs as an unprivileged account inside a
            # NoNewPrivileges/systemd filesystem sandbox.  Chrome's nested
            # setuid sandbox cannot initialize there; request routing below
            # still rejects credentials, non-HTTPS and non-public addresses
            args=['--disable-dev-shm-usage','--no-sandbox'])
        page = browser.new_page(viewport={'width':1440,'height':1000}, device_scale_factor=1)
        @lru_cache(maxsize=256)
        def allowed_host(host):
            from .network import public_addresses
            public_addresses(host)
            return True
        def safe_route(route):
            parsed=urlsplit(route.request.url)
            if parsed.scheme in {'data','blob','about'}:
                route.continue_();return
            try:
                allowed=(parsed.scheme=='https' and parsed.hostname and not parsed.username and
                         not parsed.password and parsed.port in (None,443) and allowed_host(parsed.hostname))
            except (ValueError,OSError):allowed=False
            route.continue_() if allowed else route.abort()
        page.route('**/*',safe_route)
        page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
        try:page.wait_for_load_state('networkidle', timeout=min(timeout_ms,10000))
        except Exception:pass
        # Trigger ordinary lazy loaders without depending on a site's framework
        page.evaluate("""async () => {
          for (let y=0; y<document.documentElement.scrollHeight; y+=800) {
            window.scrollTo(0,y); await new Promise(r=>setTimeout(r,80));
          }
          window.scrollTo(0,0); await new Promise(r=>setTimeout(r,250));
        }""")
        rows = page.evaluate(r"""() => {
          const selector='img,svg,canvas,video,audio,iframe,table,object,embed,a[href]';
          const seen=new Set(); const out=[];
          for (const el of document.querySelectorAll(selector)) {
            const rect=el.getBoundingClientRect(), cs=getComputedStyle(el);
            if (rect.width<1 || rect.height<1 || cs.display==='none' || cs.visibility==='hidden') continue;
            let kind=el.tagName.toLowerCase();
            if (kind==='img'||kind==='svg') kind='image';
            if (kind==='iframe'||kind==='object'||kind==='embed') kind='embed';
            if (kind==='a') kind='link';
            const target=el.currentSrc||el.src||el.href||el.data||'';
            const signature=[kind,target,Math.round(rect.x),Math.round(rect.y),Math.round(rect.width),Math.round(rect.height)].join('|');
            if (seen.has(signature)) continue; seen.add(signature);
            const id='web-object-'+String(out.length+1).padStart(4,'0');
            el.setAttribute('data-sourceloom-capture-id',id);
            el.setAttribute('data-sourceloom-capture-kind',kind);
            out.push({id,kind,target,poster:el.poster||'',alt:el.alt||el.getAttribute('aria-label')||'',
              scope:el.closest('article,main')?'article':'page',
              text:(el.innerText||el.textContent||'').trim().slice(0,4000),
              x:rect.x+scrollX,y:rect.y+scrollY,width:rect.width,height:rect.height,
              tag:el.tagName.toLowerCase()});
          }
          for (const el of document.querySelectorAll('*')) {
            const cs=getComputedStyle(el), rect=el.getBoundingClientRect();
            if (rect.width<8||rect.height<8||cs.display==='none'||cs.visibility==='hidden') continue;
            if (cs.backgroundImage!=='none' && !el.closest('[data-sourceloom-capture-id]')) {
              const id='web-object-'+String(out.length+1).padStart(4,'0');
              const match=cs.backgroundImage.match(/url\(["']?(.*?)["']?\)/);
              el.setAttribute('data-sourceloom-capture-id',id);
              el.setAttribute('data-sourceloom-capture-kind','image');
              out.push({id,kind:'image',target:match?match[1]:'',poster:'',
                alt:el.getAttribute('aria-label')||'',scope:el.closest('article,main')?'article':'page',
                text:(el.innerText||el.textContent||'').trim().slice(0,4000),
                x:rect.x+scrollX,y:rect.y+scrollY,width:rect.width,height:rect.height,
                tag:el.tagName.toLowerCase(),captureMode:'background'});
              continue;
            }
            if (cs.animationName==='none' || parseFloat(cs.animationDuration||'0')<=0) continue;
            if (el.closest('[data-sourceloom-capture-id]')) continue;
            const id='web-object-'+String(out.length+1).padStart(4,'0');
            el.setAttribute('data-sourceloom-capture-id',id);
            el.setAttribute('data-sourceloom-capture-kind','animation');
            out.push({id,kind:'animation',target:'',poster:'',alt:el.getAttribute('aria-label')||'',
              scope:el.closest('article,main')?'article':'page',
              text:(el.innerText||el.textContent||'').trim().slice(0,4000),
              x:rect.x+scrollX,y:rect.y+scrollY,width:rect.width,height:rect.height,tag:el.tagName.toLowerCase()});
          }
          return out;
        }""")
        assets=[]
        for row in rows:
            if (row['kind'] not in {'canvas','video','audio','embed','animation'} and
                    row.get('tag')!='svg' and row.get('captureMode')!='background'):continue
            try:
                data=page.locator('[data-sourceloom-capture-id="'+row['id']+'"]').screenshot(
                    type='png',timeout=5000,animations='allow')
            except Exception:
                continue
            name='web-captures/'+hashlib.sha256((url+'#'+row['id']).encode()).hexdigest()+'.png'
            row['preview_name']=name;assets.append((name,data))
        html=page.content().encode('utf-8')
        final=page.url
        browser.close()
    return html,final,rows,assets
