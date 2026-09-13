"""Native preview math and the target editor's documented storage form."""

import html

from bs4 import BeautifulSoup
from latex2mathml.converter import convert
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin


def markdown_renderer(target='preview'):
    def formula(text,options):
        display=bool(options.get('display_mode'))
        if len(text)>12000:
            raise ValueError('单条公式超过排版范围，原始表达式已保留')
        if target=='readweave':
            left,right=('\\[','\\]') if display else ('\\(','\\)')
            return '<span class="math-tex">'+html.escape(left+text+right)+'</span>'
        try:
            parsed=BeautifulSoup(convert(text,display='block' if display else 'inline'),'html.parser')
        except Exception as exc:
            raise ValueError('公式无法可靠排版，原始表达式已保留') from exc
        root=parsed.find('math')
        semantics=parsed.new_tag('semantics')
        for child in list(root.contents):
            semantics.append(child.extract())
        annotation=parsed.new_tag('annotation',encoding='application/x-tex')
        annotation.string=text
        semantics.append(annotation)
        root.append(semantics)
        return str(root)
    return MarkdownIt('commonmark',{'html':True}).enable('table').use(
        dollarmath_plugin,allow_labels=False,allow_space=False,renderer=formula)
