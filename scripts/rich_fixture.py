"""Original synthetic package exercises objects, not model teaching quality."""
from io import BytesIO
from pathlib import Path
import json
from PIL import Image,ImageDraw
from sourceloom.store import Store
from sourceloom.ingest import intake
from sourceloom.checks import freeze
from sourceloom.export import export_zip


def create(store):
    p=store.create('混合对象保真探针 · 合成材料',budget=0)
    image=Image.new('RGB',(320,180),'white');draw=ImageDraw.Draw(image)
    draw.line([(30,15),(30,150),(300,150)],fill='black',width=2)
    draw.line([(30,135),(110,100),(190,65),(270,30)],fill='black',width=3)
    draw.text((50,155),'SYNTHETIC DATA',fill='black');buf=BytesIO();image.save(buf,'PNG')
    html='''<h1>Synthetic object retention fixture</h1>
<p>All values are teaching assumptions. Only the stated conditions apply <a href="#fn1">[1]</a></p>
<figure><img src="plot.png" alt="Synthetic ascending line"><figcaption>First occurrence of the original chart</figcaption></figure>
<table><caption>Assumed durations</caption><tr><th rowspan="2">Part</th><th>Value</th></tr><tr><td>milliseconds</td></tr><tr><td>Fixed</td><td>20</td></tr><tr><td>Variable</td><td>80</td></tr></table>
<pre><code>total = fixed + variable / factor
assert factor &gt; 0</code></pre>
<p>Formula source: T = 20 + 80 / k, where k is a positive dimensionless ratio</p>
<p id="fn1" role="doc-footnote">[1] Extra overhead is excluded only in this synthetic example</p>
<p><a href="https://example.invalid/source#conditions">Preserved example link, not fetched research evidence</a></p>
<figure><img src="plot.png" alt="Repeated original chart"><figcaption>Second occurrence, same bytes and distinct position</figcaption></figure>'''
    inv=freeze(intake(store,[('fixture.html',html.encode()),('plot.png',buf.getvalue())]))
    unit={'id':'objects','title':'逐项保留对象','objective':'核对格式与出现位置','obligation_ids':[o['id'] for o in inv['obligations']],
          'prerequisites':[],'stages':['问题','前提','关系','机制','示例','边界','拓展'],'object_ids':[o['id'] for o in inv['objects']],'proof_questions':[]}
    draft={'blocks':[{'id':'block-'+o['id'],'unit_id':'objects','kind':'source','markdown':'',
                      'obligation_ids':[v['id'] for v in inv['obligations'] if v['object_id']==o['id']],
                      'object_ids':[o['id']],'evidence':[]} for o in inv['objects']]}
    return store.change(p['id'],lambda p:p.update(inventory=inv,plan={'title':'对象保留','objective':'合成验证','units':[unit],'research_gaps':[]},
                                               draft=draft,revision=1,state='generated',synthetic=True,fixture='rich-objects'))

def main():
    out=Path('.local/rich-fixture');out.mkdir(exist_ok=True)
    if (out/'project.json').exists():raise SystemExit('Fixture exists; reuse its identity')
    store=Store(out/'data');p=create(store)
    (out/'project.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'ReadWeave-rich-candidate.zip').write_bytes(export_zip(store,p))
    print(json.dumps({'objects':len(p['inventory']['objects']),'image_occurrences':sum(o['kind']=='image' for o in p['inventory']['objects'])}))

if __name__=='__main__':main()
