import { CLIPS, VOD } from '#/content'

const W = 4 // px per minute in the viewBox
const HOURS = Array.from({ length: Math.floor(VOD.length / 60) + 1 }, (_, h) => h)

function stamp(min: number) {
  const h = Math.floor(min / 60)
  return `${h}:${String(min % 60).padStart(2, '0')}:00`
}

// The four-hour VOD as Clipit sees it: loudness up, chat down, the picked windows boxed.
export function Timeline() {
  return (
    <svg className="timeline" viewBox={`0 0 ${VOD.length * W} 214`} role="img"
      aria-label="A four-hour stream's loudness and chat activity, with three picked clip windows">
      <line x1="0" x2={VOD.length * W} y1="120" y2="120" className="tl-axis" />
      {VOD.map((m, i) => (
        <g key={i}>
          <rect x={i * W} y={120 - m.loud * 80} width={W - 1.5} height={m.loud * 80} className="tl-loud" />
          <rect x={i * W} y={122} width={W - 1.5} height={m.chat * 56} className="tl-chat" />
        </g>
      ))}
      {HOURS.map((h) => (
        <text key={h} x={h * 60 * W} y="210" className="tl-hour" textAnchor={h === 0 ? 'start' : h === HOURS.length - 1 ? 'end' : 'middle'}>
          {h}h
        </text>
      ))}
      {CLIPS.map(({ at: p }, i) => (
        <g key={p} className="tl-pick" style={{ '--i': i } as React.CSSProperties}>
          <rect x={p * W - 4} y="18" width={4 * W + 8} height="168" rx="4" />
          <text x={p * W - 4} y="10">{stamp(p)}</text>
        </g>
      ))}
      <line className="tl-scan" x1="0" x2="0" y1="14" y2="190" style={{ '--tl-w': `${VOD.length * W}px` } as React.CSSProperties} />
    </svg>
  )
}
