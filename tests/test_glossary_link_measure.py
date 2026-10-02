from sourceloom.active_composition import overgrown_short_rewrite_glossary
from sourceloom.writing import canonical


def _article_with_links(definition_body, labels, url_size=400):
    objects=[
        dict(kind='heading',text='Care With Font Size'),
        dict(kind='text',text='Small type can make important material harder to read.'),
        dict(kind='text',text='Readers use screens with different dimensions and settings.'),
    ]
    for index,label in enumerate(labels):
        objects.append(dict(kind='link',text=label))
    definitions='\n'.join(
        f'- {term}（{english}）：{definition_body}'
        for term,english in [('对比度','contrast ratio'),('文本','text'),('字号','font size')]
    )
    references='References: '+' '.join(
        f'[ {label} ](https://source.example/section-{"x"*url_size}(detail-{index})#wcag)'
        for index,label in enumerate(labels)
    )
    return objects, {'blocks':[dict(markdown=definitions+'\n\n'+references)]}


def test_long_markdown_destinations_do_not_trigger_short_rewrite_glossary_gate():
    labels=['WCAG source 1','WCAG source 2','WCAG source 3']
    objects,draft=_article_with_links('简短说明。',labels)
    source_chars=sum(len(obj['text']) for obj in objects if obj['kind'] in {'text','heading'})
    allowance=source_chars*2.5+len(labels)*100

    assert len(canonical(draft))>allowance
    assert not overgrown_short_rewrite_glossary(draft,objects)


def test_visible_glossary_expansion_still_triggers_with_long_destinations():
    labels=['WCAG source 1','WCAG source 2','WCAG source 3']
    objects,draft=_article_with_links('详细定义'*60,labels)

    assert overgrown_short_rewrite_glossary(draft,objects)


def test_markdown_link_labels_remain_part_of_visible_length():
    labels=[f'visible reference {index} '+'说明'*100 for index in range(3)]
    objects,draft=_article_with_links('简短说明。',labels)

    assert overgrown_short_rewrite_glossary(draft,objects)
