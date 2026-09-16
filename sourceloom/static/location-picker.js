// A bounded, keyboard-operable disclosure; the full excerpt stays in its row.
export function locationPicker(label, items, onSelect) {
  const picker = document.createElement('details');
  picker.className = 'location-picker';
  const summary = document.createElement('summary');
  summary.textContent = label;
  const list = document.createElement('div');
  list.className = 'location-options';
  items.forEach((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    const title = document.createElement('strong');
    title.textContent = `${index + 1}. ${item.title}`;
    const excerpt = document.createElement('span');
    excerpt.textContent = item.excerpt;
    button.append(title, excerpt);
    button.addEventListener('click', () => {
      picker.open = false;
      summary.textContent = title.textContent;
      summary.focus();
      onSelect(item.value);
    });
    list.append(button);
  });
  picker.append(summary, list);
  picker.addEventListener('keydown', event => {
    if (event.key === 'Escape') { picker.open = false; summary.focus(); }
  });
  return picker;
}
