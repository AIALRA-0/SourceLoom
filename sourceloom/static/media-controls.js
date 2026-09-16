// Disclosure preferences belong to a material, without reloading its article.
export function mediaControls(frames, input, documentId) {
  frames = (Array.isArray(frames) ? frames : [frames]).filter(Boolean);
  if (!frames.length || !input) return;
  const key = 'loom-media-expanded-' + documentId;
  let applying = false;
  const attachedDocuments = new Map();
  const regions = () => frames.flatMap(frame => [...(frame.contentDocument?.querySelectorAll('details[data-media]') || [])]);
  function reflect() {
    if (applying) return;
    const items = regions(), count = items.filter(item => item.open).length;
    input.disabled = items.length === 0;
    input.checked = items.length > 0 && count === items.length;
    input.indeterminate = count > 0 && count < items.length;
    input.closest('label').title = items.length ? '切换原文和改写正文中全部配图的展开状态' : '当前材料没有可展开的配图';
  }
  function apply(expanded) {
    applying = true;
    for (const item of regions()) item.open = expanded;
    applying = false;
    reflect();
  }
  function loaded(frame) {
    const doc = frame.contentDocument;
    if (!doc || doc.URL === 'about:blank' || doc.URL !== frame.src || doc === attachedDocuments.get(frame)) return;
    if (doc.readyState === 'loading') return;
    attachedDocuments.set(frame, doc);
    for (const item of doc.querySelectorAll('details[data-media]')) item.open = localStorage.getItem(key) === 'true';
    reflect();
    doc.addEventListener('toggle', reflect, true);
  }
  input.addEventListener('change', () => {
    localStorage.setItem(key, String(input.checked));
    apply(input.checked);
  });
  for (const frame of frames) {
    frame.addEventListener('load', () => loaded(frame));
    // A lazy original preview starts later; its load listener remains available.
    let checks = 0, timer;
    function ready() {
      clearTimeout(timer);
      if (!frame.isConnected || !input.isConnected) return;
      loaded(frame);
      if ((attachedDocuments.get(frame) !== frame.contentDocument || frame.contentDocument?.URL !== frame.src) && ++checks < 600 && frame.getAttribute('src')) timer = setTimeout(ready, 100);
    }
    const navigation = new MutationObserver(() => { checks = 0; ready(); });
    navigation.observe(frame, { attributes: true, attributeFilter: ['src'] });
    ready();
  }
}
