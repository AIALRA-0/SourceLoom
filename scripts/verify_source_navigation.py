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
        browser.close()
        return result


if __name__=='__main__':
    print(json.dumps(verify()))
