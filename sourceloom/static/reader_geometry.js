// Local content anchors only. Reordered ranges never authorize interpolation.
export const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
export function locate(rows, point) {
  if (!rows.length) return null;
  let low=0, high=rows.length;
  while(low<high) {const mid=(low+high)>>>1; if(rows[mid].top<=point) low=mid+1; else high=mid;}
  return rows[Math.max(0,low-1)];
}
export function mapPoint(anchors, point, side, follower = 0, ordered = false) {
  const other=side==='left'?'right':'left';
  const sorted=ordered ? anchors : anchors.filter(a=>Number.isFinite(a[side])&&Number.isFinite(a[other]))
    .sort((a,b)=>a[side]-b[side] || a[other]-b[other]);
  if(!sorted.length) return null;
  let low=0,high=sorted.length;
  while(low<high) {const mid=(low+high)>>>1;if(sorted[mid][side]<point)low=mid+1;else high=mid;}
  const next=sorted[Math.min(low,sorted.length-1)],prev=sorted[Math.max(0,low-1)];
  if(Math.abs(next[side]-point)<.5) {
    let matches=sorted.filter(a=>Math.abs(a[side]-point)<.5);
    const direct=matches.filter(a=>a.precision==='manual'||a.precision==='region');
    if(direct.length)matches=direct;
    const choice=matches.reduce((a,b)=>Math.abs(a[other]-follower)<=Math.abs(b[other]-follower)?a:b);
    return {...choice,position:choice[other]};
  }
  // Duplicate original regions can have several article occurrences. Match
  // both endpoints of the same occurrence before choosing the nearest one.
  const before=[],after=[];
  for(let i=low-1;i>=0&&sorted[i][side]===prev[side];i--)before.push(sorted[i]);
  for(let i=low;i<sorted.length&&sorted[i][side]===next[side];i++)after.push(sorted[i]);
  let candidates=[];
  for(const a of before)for(const b of after) {
    if(!a.blockId||a.blockId!==b.blockId||b[other]<a[other]||Math.abs(b.page-a.page)>1)continue;
    const progress=clamp((point-a[side])/(b[side]-a[side]));
    candidates.push({position:a[other]+(b[other]-a[other])*progress,
      precision:a.precision===b.precision?a.precision:'page',blockId:a.blockId});
  }
  const regions=candidates.filter(a=>a.precision==='region'||a.precision==='manual');
  if(regions.length)candidates=regions;
  if(candidates.length)return candidates.reduce((a,b)=>Math.abs(a.position-follower)<=Math.abs(b.position-follower)?a:b);
  if(prev!==next && next[other]>=prev[other] &&
      ((prev.blockId && prev.blockId===next.blockId) || (prev.range && prev.range===next.range)) &&
      Math.abs(next.page-prev.page)<=1) {
    const progress=clamp((point-prev[side])/(next[side]-prev[side]));
    return {position:prev[other]+(next[other]-prev[other])*progress,
      precision:prev.precision===next.precision?prev.precision:'page',blockId:prev.blockId};
  }
  const near=Math.abs(point-prev[side])<=Math.abs(point-next[side])?prev:next;
  return {...near,position:near[other],precision:near.precision==='region'?'region':'page'};
}
