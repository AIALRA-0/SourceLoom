// Appearance state is independent of material and model state.
const KEY = 'sourceloom-workbench/1';
export function readPreferences(storage) {
  try {
    const value = JSON.parse((storage ?? globalThis.localStorage).getItem(KEY) || '{}');
    return { theme: ['system', 'light', 'dark'].includes(value.theme) ? value.theme : 'system',
      density: ['comfortable', 'compact'].includes(value.density) ? value.density : 'comfortable',
      inspector: value.inspector === true };
  } catch { return {theme:'system',density:'comfortable',inspector:false}; }
}
export function applyTheme(root, prefs, dark = false) {
  root.dataset.theme = prefs.theme === 'system' ? (dark ? 'dark' : 'light') : prefs.theme;
  root.dataset.density = prefs.density;
}
export {KEY as appearanceKey};
