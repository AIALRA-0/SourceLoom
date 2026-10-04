// Presentation uses the server's explicit route. Missing metadata never grants
// permission, and another provider is not a fallback for a blocked Web route.
export function webChannels(raw = {}) {
  const channels = Array.isArray(raw) ? raw : Object.entries(raw && typeof raw === 'object' ? raw : {}).map(([id, value]) =>
    value && typeof value === 'object' ? {...value, id} : {id, available: false});
  return channels.filter(channel => channel && channel.id === 'router').map(channel => {
    const permitted = channel.execution_channel === 'chatgpt_web' && channel.mode === 'chat';
    return {...channel, label: 'Web Chat', available: permitted && channel.available === true,
      model: permitted ? channel.model : null,
      reason: permitted ? channel.reason : '自动生成仅允许已明确配置的 Web Chat 普通 chat 模式；不会切换凭据或使用其他引擎。'};
  });
}

export function webGenerationChannel(raw, selected, requiresVisual = false) {
  const channel = webChannels(raw).find(item => item.id === selected);
  if (!channel || !channel.available || (requiresVisual && channel.supports_visual !== true)) {
    throw new Error('当前 Web Chat 通道不满足此材料的交接要求；未发送请求，可继续手动交接。');
  }
  return channel.id;
}
