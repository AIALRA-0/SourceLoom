// The original iframe is a read-only view. A CSS Highlight marks a DOM range
// without rewriting source bytes, links, or the document's text nodes.
export function bindSourceClicks(frame, onBlock) {
  frame.addEventListener('load', () => {
    const doc = frame.contentDocument;
    if (!doc || doc.body.dataset.sourceClicks === 'ready') return;
    doc.body.dataset.sourceClicks = 'ready';
    const style = doc.createElement('style');
    style.textContent = 'section[data-readweave-anchor-id]:hover{outline:1px solid #d8d8cc;outline-offset:6px;cursor:pointer}';
    doc.head.append(style);
    doc.addEventListener('click', event => {
      if (event.target.closest('a,button')) return;
      const section = event.target.closest('section[data-readweave-anchor-id]');
      if (section) onBlock(section.dataset.readweaveAnchorId);
    });
    doc.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const section = event.target.closest('section[data-readweave-anchor-id]');
      if (section) { event.preventDefault(); onBlock(section.dataset.readweaveAnchorId); }
    });
    for (const section of doc.querySelectorAll('section[data-readweave-anchor-id]')) section.tabIndex = 0;
  });
}

function normalized(text) { return text.replace(/\s+/gu, ' ').trim(); }

function findVisibleRange(doc, quote) {
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) { return node.parentElement?.closest('script,style,noscript') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; }
  });
  const nodes = [];
  let raw = '';
  while (walker.nextNode()) {
    const node = walker.currentNode;
    nodes.push({node, start: raw.length, end: raw.length + node.data.length});
    raw += node.data;
  }
  const target = normalized(quote);
  if (!target || !raw) return null;
  let compact = '';
  const offsets = [];
  for (let i = 0; i < raw.length; i++) {
    const char = raw[i];
    if (/\s/u.test(char)) {
      if (compact && compact[compact.length - 1] !== ' ') { compact += ' '; offsets.push(i); }
    } else { compact += char; offsets.push(i); }
  }
  let index = compact.indexOf(target);
  let length = target.length;
  if (index < 0 && target.length > 100) { length = 100; index = compact.indexOf(target.slice(0, length)); }
  if (index < 0) return null;
  const start = offsets[index], end = offsets[index + length - 1] + 1;
  const first = nodes.find(item => item.start <= start && start < item.end);
  const last = nodes.find(item => item.start < end && end <= item.end);
  if (!first || !last) return null;
  const range = doc.createRange();
  range.setStart(first.node, start - first.start);
  range.setEnd(last.node, end - last.start);
  return range;
}

export async function focusOriginal(frame, pid, location) {
  const base = `/api/projects/${encodeURIComponent(pid)}/original-view/${encodeURIComponent(location.original_key)}`;
  if (location.format === 'pdf') {
    const target = base + (location.page ? `#page=${location.page}` : '');
    if (frame.getAttribute('src') !== target) frame.setAttribute('src', target);
    return location.page ? 'page' : 'file';
  }
  if (!frame.getAttribute('src')?.startsWith(base)) {
    await new Promise(resolve => {
      frame.addEventListener('load', resolve, {once:true});
      frame.setAttribute('src', base);
    });
  }
  const doc = frame.contentDocument;
  if (!doc) return 'file';
  const range = findVisibleRange(doc, location.quote);
  if (!range) return 'file';
  const view = doc.defaultView;
  if (view.CSS?.highlights && view.Highlight) {
    let style = doc.getElementById('sourceloom-source-highlight');
    if (!style) {
      style = doc.createElement('style'); style.id = 'sourceloom-source-highlight';
      style.textContent = '::highlight(sourceloom-source){background:rgba(221,205,144,.45);text-decoration:underline;text-decoration-color:#c7b365;text-decoration-thickness:2px}';
      doc.head.append(style);
    }
    view.CSS.highlights.set('sourceloom-source', new view.Highlight(range));
  } else {
    const selection = view.getSelection(); selection.removeAllRanges(); selection.addRange(range);
  }
  range.startContainer.parentElement?.scrollIntoView({block:'center',behavior:'smooth'});
  return 'highlight';
}
