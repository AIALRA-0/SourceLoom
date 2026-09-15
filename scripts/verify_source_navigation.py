"""Real-browser regression for exact source ranges, including long mixed text.

Run with the test extras and an installed Playwright Chromium browser:
    python scripts/verify_source_navigation.py
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def verify():
    source=(Path(__file__).resolve().parents[1]/'sourceloom/static/source-focus.js').read_text(encoding='utf-8')
    functions=source[:source.index('function mark(')]
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page()
        page.set_content('<main><p>  first <em>second\t</em><b>  三😀</b> end</p>'
                         '<p>same same</p><script>excluded script</script><style>.unused{}</style></main>')
        result=page.evaluate('''source=>{eval(source+`;globalThis.testIndex=indexDocument;globalThis.testRanges=rangesFor`);
          const index=testIndex(document);
          const spanning=testRanges(document,'first second 三😀 end');
          if(spanning.length!==1||spanning[0].startOffset!==2||spanning[0].toString().replace(/\\s+/gu,' ')!=='first second 三😀 end')throw Error('Cross-node/Unicode range changed');
          if(testRanges(document,'same').length!==2)throw Error('Repeated quote lost');
          if(testRanges(document,'excluded script').length||testRanges(document,'missing').length)throw Error('Non-source text matched');
          if(testRanges(document,'first second 三😀 end')!==spanning)throw Error('Range cache was not reused');
          const longDoc=document.implementation.createHTMLDocument('Long source');
          const fragment=longDoc.createDocumentFragment();
          for(let i=0;i<1200;i++){const p=longDoc.createElement('p');p.textContent=('Shared ordinary content '+i+' 中文😀 ').repeat(12)+' unique-ending-'+i+' ';fragment.append(p)}
          longDoc.body.append(fragment);const start=performance.now(),longIndex=testIndex(longDoc),elapsed=performance.now()-start;
          for(const n of [0,599,1199]){const target='unique-ending-'+n+' ',ranges=testRanges(longDoc,target);if(ranges.length!==1||ranges[0].toString()!==target.trim())throw Error('Long-document location drifted')}
          return {cross_node_unicode:true,repeated_quotes:true,excluded_non_source:true,range_cache:true,long_source_chars:longIndex.text.length,index_ms:Math.round(elapsed),offset_storage_bytes:longIndex.offsets.byteLength,long_nodes:longIndex.nodes.length};
        }''',functions)
        page.set_content('<iframe srcdoc="&lt;section data-readweave-anchor-id=one&gt;&lt;details&gt;&lt;summary&gt;Original page&lt;/summary&gt;Original text&lt;/details&gt;&lt;/section&gt;"></iframe>')
        page.frame_locator('iframe').locator('summary').wait_for()
        page.evaluate('''source=>{eval(source.replace(/export /g,'')+`;globalThis.bindTest=bindSourceClicks`);
          globalThis.blockClicks=0;bindTest(document.querySelector('iframe'),()=>blockClicks++);
        }''',source)
        page.frame_locator('iframe').locator('summary').focus()
        page.keyboard.press('Enter')
        assert page.frame_locator('iframe').locator('details').get_attribute('open') is not None
        assert page.evaluate('blockClicks')==0
        page.frame_locator('iframe').locator('section').focus()
        page.keyboard.press('Enter')
        assert page.evaluate('blockClicks')==1
        result['keyboard_reference_toggle']=True
        result['keyboard_block_navigation']=True
        browser.close()
        return result


if __name__=='__main__':
    print(json.dumps(verify()))
