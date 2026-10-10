"""Native preview math and the target editor's documented storage form."""

import html
import re

from bs4 import BeautifulSoup
from latex2mathml.converter import convert
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.texmath.index import make_block_func, make_inline_func, rules as tex_rules


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
    md = MarkdownIt('commonmark', {'html': True}).enable('table').use(
        dollarmath_plugin, allow_labels=False, allow_space=False, renderer=formula)
    # The writing template also permits TeX's \(...\) and \[...\] delimiters.
    # Parse them before Markdown treats the backslash as a generic escape. The
    # renderer is the same as for dollar math, so preview and ReadWeave share
    # one formula representation rather than showing literal brackets.
    for rule in tex_rules['brackets']['inline']:
        bracket_rule = {**rule, 'name': 'bracket_' + rule['name']}
        md.inline.ruler.before('escape', bracket_rule['name'], make_inline_func(bracket_rule))
        md.add_render_rule(bracket_rule['name'],
                           lambda renderer, tokens, idx, options, env:
                           formula(tokens[idx].content, {'display_mode': False}))
    for rule in tex_rules['brackets']['block']:
        bracket_rule = {**rule, 'name': 'bracket_' + rule['name']}
        md.block.ruler.before('fence', bracket_rule['name'], make_block_func(bracket_rule))
        if rule['name'] == 'math_block_eqno':
            md.add_render_rule(bracket_rule['name'],
                               lambda renderer, tokens, idx, options, env:
                               formula(tokens[idx].content, {'display_mode': True}) +
                               '<span class="equation-number">(' +
                               html.escape(tokens[idx].info) + ')</span>')
        else:
            md.add_render_rule(bracket_rule['name'],
                               lambda renderer, tokens, idx, options, env:
                               formula(tokens[idx].content, {'display_mode': True}))
    # A return may wrap a complete display formula in a code container. Interpret
    # only math-labelled fences or a whole delimited expression; programming
    # fences and mixed prose remain literal. Stored Markdown is never rewritten.
    for kind in ('fence', 'code_block'):
        original = md.renderer.rules[kind]
        def render_math_container(renderer, tokens, idx, options, env, fallback=original):
            token = tokens[idx]
            language = token.info.strip().lower()
            content = token.content.strip()
            delimited = re.fullmatch(r'\$\$\s*([\s\S]+?)\s*\$\$', content)
            if not delimited:
                delimited = re.fullmatch(r'\\\[\s*([\s\S]+?)\s*\\\]', content)
            if language in ('math', 'latex', 'tex') or (not language and delimited):
                expression = delimited.group(1) if delimited else content
                try:
                    return formula(expression, {'display_mode': True}) + '\n'
                except ValueError:
                    return '<div class="math-render-fallback" role="note">公式暂无法排版，保留原始表达式</div>' + fallback(tokens, idx, options, env)
            return fallback(tokens, idx, options, env)
        md.add_render_rule(kind, render_math_container)
    return md
