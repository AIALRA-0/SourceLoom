"""Clearly labelled synthetic artifacts for a free, executable walkthrough."""

from .checks import freeze, validate_plan
from .ingest import intake

SAMPLE = """# 合成示例：局部加速的边界

一次固定工作量任务原本需要 100 毫秒，其中 20 毫秒不能加速，80 毫秒可以加速

只把可加速部分的速度提高到原来的 2 倍，假设没有额外开销，新耗时为 60 毫秒

这不等于整个任务快 2 倍，也不能据此推断新增通信开销后的结果

| 部分 | 原耗时 | 新耗时 |
| --- | --- | --- |
| 不可加速 | 20 毫秒 | 20 毫秒 |
| 可加速 | 80 毫秒 | 40 毫秒 |

以上数字是教学假设，不是论文实测
"""


def create_demo(store):
    p=store.create("局部加速的边界 · 合成演示",budget=0)
    inv=freeze(intake(store,[("example.md",SAMPLE.encode())]))
    unit=dict(id="unit-1",title="为什么局部快两倍，整体却没有",objective="从组成部分算出总耗时，并识别成立条件",
              obligation_ids=[o["id"] for o in inv["obligations"]],prerequisites=["理解时间相加与平均分成两份"],
              stages=["确定实际问题","说明固定工作量和无额外开销","区分两部分耗时","逐步计算新总时间","核对 60 毫秒的结果","增加 10 毫秒开销后重算","拓展多个部分但保留条件"],
              object_ids=[o["id"] for o in inv["objects"]],proof_questions=["为什么新时间不是 50 毫秒"])
    plan=validate_plan(dict(title=p["title"],objective=unit["objective"],units=[unit],research_gaps=[]),inv)
    blocks=[]
    for n,o in enumerate(inv["objects"]):
        blocks.append(dict(id=f"block-{n+1}",unit_id="unit-1",kind="object" if o["kind"]=="table" else "source",
                           markdown="" if o["kind"]=="table" else ("# "+o["text"] if o["kind"]=="heading" else o["text"]),obligation_ids=[inv["obligations"][n]["id"]],
                           object_ids=[o["id"]] if o["kind"] in {"table","link","image","formula","code","heading"} else [],
                           evidence=[dict(source_id=o["id"],quote=o["text"])] if o["text"] else []))
    blocks.extend([
        dict(id="example-1",unit_id="unit-1",kind="example",markdown="### 一步一步计算\n\n- 第一步，保留不能加速部分的 20 毫秒\n- 第二步，把可加速部分的 80 毫秒平均分成两份，得到 40 毫秒\n- 第三步，把 20 毫秒与 40 毫秒相加，得到 60 毫秒\n\n只有后一个部分变快，所以整体耗时不会减半",obligation_ids=[],object_ids=[],evidence=[]),
        dict(id="exercise-1",unit_id="unit-1",kind="exercise",markdown="### 条件变了以后\n\n如果新增 10 毫秒通信开销，新总时间是多少？\n\n先取刚才得到的 60 毫秒，再加新增的 10 毫秒，得到 70 毫秒\n\n这是另一个教学假设，不能将无额外开销的结果直接当作新场景的结果",obligation_ids=[],object_ids=[],evidence=[]),
        dict(id="extension-1",unit_id="unit-1",kind="extension",markdown="### 继续研究的入口\n\n如果一个任务有多个能够分别加速的部分，可以分别计算各部分的新耗时，再相加；前提是这些部分仍然依次执行，而且额外开销已经单独计入",obligation_ids=[],object_ids=[],evidence=[])
    ])
    def update(p):
        p.update(inventory=inv,plan=plan,draft={"blocks":blocks},revision=1,state="generated",synthetic=True)
    return store.change(p["id"],update)
