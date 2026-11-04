import { useEffect, useState } from 'react'
import { CLIPS } from '#/content'

type Clip = (typeof CLIPS)[number]

const WORD_MS = 420

// A drawn stand-in for a rendered 9:16 clip: facecam on top, gameplay below, hook and captions.
// Captions light up word by word, like the real renders; reduced motion keeps the clip's key word lit.
// One timer drives both the caption and the progress bar, which restarts on each loop, so they can't drift apart.
export function ClipFrame({ clip, className = '' }: { clip: Clip; className?: string }) {
  const [tick, setTick] = useState<number | null>(null)

  useEffect(() => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
    setTick(0)
    const id = setInterval(() => setTick((t) => (t ?? 0) + 1), WORD_MS)
    return () => clearInterval(id)
  }, [])

  const word = tick === null ? clip.hot : tick % clip.words.length
  const loop = tick === null ? 0 : Math.floor(tick / clip.words.length)

  return (
    <figure
      className={`clip-frame ${className}`}
      style={{ '--h': clip.hue, '--loop': `${clip.words.length * WORD_MS}ms` } as React.CSSProperties}
    >
      <div className="clip-cam">
        <span className="clip-face" />
      </div>
      <div className="clip-game">
        <span className="clip-hook">{clip.hook}</span>
        <span className="clip-caption" aria-hidden>
          {clip.words.map((w, i) => (
            <span key={i} className={i === word ? 'is-hot' : undefined}>
              {w}{' '}
            </span>
          ))}
        </span>
      </div>
      {tick !== null && <span key={loop} className="clip-progress" />}
      <figcaption className="sr-only">
        Example clip: {clip.hook}. Captions: {clip.words.join(' ')}
      </figcaption>
    </figure>
  )
}
